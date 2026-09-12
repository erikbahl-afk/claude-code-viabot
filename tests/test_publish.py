"""The upload path, exercised against a real server over real HTTP.

These tests exist because the requirement is not "uploads files" — it is
"survives having the power cut mid-upload", and that is not something a mock
can be asked about honestly. So each of these starts the actual receiver,
sends actual bytes, interrupts at an awkward moment, throws away every object
that held state, and checks that what arrives is byte-identical to what left.
"""

from __future__ import annotations

import hashlib
import importlib.util
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from viabot_survey.publish import PublishClient, PublishError
from viabot_survey.storage import Storage
from viabot_survey.workers.publisher import PublisherWorker

RECEIVER = Path(__file__).resolve().parent.parent / "server" / "viabot_receiver.py"
TOKEN = "test-token-not-a-real-one"


def _load_receiver():
    spec = importlib.util.spec_from_file_location("viabot_receiver", RECEIVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def receiver(tmp_path):
    """A real receiver on a real port, in a thread."""
    module = _load_receiver()
    module.DATA_DIR = tmp_path / "server"
    module.UPLOAD_TOKEN = TOKEN
    module.VIEWER_PASSWORD = ""
    module.DATA_DIR.mkdir(parents=True, exist_ok=True)

    server = make_server("127.0.0.1", 0, module.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    module.base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield module
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _file(path: Path, size: int) -> Path:
    """A file whose content is position-dependent, so a chunk landing at the
    wrong offset changes the digest instead of quietly matching."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes((i * 7 + 13) % 251 for i in range(size)))
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stored(receiver, run_id: str, name: str) -> Path:
    return receiver.DATA_DIR / "runs" / run_id / name


# ---- the protocol ----------------------------------------------------------

def test_a_whole_file_arrives_intact(receiver, tmp_path):
    source = _file(tmp_path / "clip.mp4", 300_000)
    client = PublishClient(receiver.base_url, TOKEN, chunk_bytes=64 * 1024)

    client.upload("run-1", "clips/clip.mp4", source)

    landed = _stored(receiver, "run-1", "clips/clip.mp4")
    assert landed.exists()
    assert _digest(landed) == _digest(source)


def test_an_unfinished_upload_is_not_served_as_if_it_were_whole(receiver, tmp_path):
    """A half-written clip that looks like a clip is worse than a missing one:
    somebody clicks it, it plays for four seconds, and they believe that is all
    the footage there was."""
    source = _file(tmp_path / "clip.mp4", 300_000)
    client = PublishClient(receiver.base_url, TOKEN, chunk_bytes=64 * 1024)

    sent = [0]

    def stop_after_two_chunks():
        sent[0] += 1
        return sent[0] > 2

    client.upload("run-1", "clips/clip.mp4", source,
                  should_stop=stop_after_two_chunks)

    assert not _stored(receiver, "run-1", "clips/clip.mp4").exists()
    assert _stored(receiver, "run-1", "clips/clip.mp4.part").exists()


def test_an_interrupted_upload_resumes_where_it_stopped(receiver, tmp_path):
    """The headline requirement. Everything that knew about the transfer is
    thrown away between the two halves, exactly as a power cut would."""
    source = _file(tmp_path / "clip.mp4", 500_000)

    first = PublishClient(receiver.base_url, TOKEN, chunk_bytes=32 * 1024)
    chunks = [0]

    def cut_the_power():
        chunks[0] += 1
        return chunks[0] > 3

    stopped_at = first.upload("run-1", "clips/clip.mp4", source,
                              should_stop=cut_the_power)
    assert 0 < stopped_at < source.stat().st_size

    del first                                     # nothing survives the cut
    second = PublishClient(receiver.base_url, TOKEN, chunk_bytes=32 * 1024)
    assert second.remote_offset("run-1", "clips/clip.mp4") == (stopped_at, False)
    second.upload("run-1", "clips/clip.mp4", source)

    landed = _stored(receiver, "run-1", "clips/clip.mp4")
    assert _digest(landed) == _digest(source)


def test_a_stale_chunk_cannot_corrupt_a_file(receiver, tmp_path):
    """A retry that arrives after the one it duplicates must be refused, not
    appended. Otherwise a flaky link silently produces a longer, broken file."""
    source = _file(tmp_path / "clip.mp4", 100_000)
    client = PublishClient(receiver.base_url, TOKEN, chunk_bytes=32 * 1024)
    payload = source.read_bytes()[:32 * 1024]

    client.send_chunk("run-1", "clips/clip.mp4", 0, 100_000, payload)
    with pytest.raises(Exception) as caught:       # OffsetConflict
        client.send_chunk("run-1", "clips/clip.mp4", 0, 100_000, payload)
    assert getattr(caught.value, "offset", None) == 32 * 1024


def test_uploads_cannot_escape_the_run_directory(receiver, tmp_path):
    """The token is on an SD card in a rig that gets carried around."""
    source = _file(tmp_path / "x.bin", 32)
    client = PublishClient(receiver.base_url, TOKEN, chunk_bytes=1024)
    with pytest.raises(PublishError):
        client.upload("run-1", "../../etc/passwd", source)


def test_a_wrong_token_is_refused(receiver, tmp_path):
    source = _file(tmp_path / "x.bin", 32)
    client = PublishClient(receiver.base_url, "not-the-token", chunk_bytes=1024)
    with pytest.raises(PublishError):
        client.upload("run-1", "clips/x.bin", source)


# ---- the queue, which is what actually survives a reboot -------------------

def _rig(tmp_path, receiver, chunk=32 * 1024, busy=lambda: False):
    """A rig's storage, client and publisher — rebuilt from disk each time, so
    a test can throw the whole lot away and start again the way a reboot does."""
    storage = Storage(tmp_path / "rig" / "survey.db")
    client = PublishClient(receiver.base_url, TOKEN, chunk_bytes=chunk)
    worker = PublisherWorker(client=client, storage=storage, busy=busy,
                             enabled=False)
    return storage, worker


def test_a_queued_run_finishes_after_the_power_comes_back(receiver, tmp_path):
    """The whole feature in one test. A walk is queued, the upload is cut off
    part way, and everything holding state — the client, the worker, the
    database connection — is discarded. What comes back has to finish the job,
    not start it again."""
    source = _file(tmp_path / "clips" / "deadzone-01.mp4", 400_000)

    storage, worker = _rig(tmp_path, receiver, busy=lambda: False)
    storage.create_run("run-1", label="Level 2")
    storage.enqueue_upload("run-1", "clip", str(source), "clips/deadzone-01.mp4",
                           source.stat().st_size)

    # Power goes as the third chunk is acknowledged.
    chunks = [0]
    worker.busy = lambda: (chunks.__setitem__(0, chunks[0] + 1), chunks[0] > 3)[1]
    worker.drain()

    row = storage.list_uploads("run-1")[0]
    assert row["state"] == "pending"
    assert 0 < row["sent_bytes"] < source.stat().st_size

    del worker, storage                     # the rig loses power here

    storage, worker = _rig(tmp_path, receiver)
    assert worker.drain() == 1
    assert storage.list_uploads("run-1")[0]["state"] == "done"
    assert _digest(_stored(receiver, "run-1", "clips/deadzone-01.mp4")) == _digest(source)


def test_a_stale_local_offset_does_not_duplicate_bytes(receiver, tmp_path):
    """Power can be cut between a chunk landing on the server and the rig
    recording that it did, leaving the rig's own number behind the truth.
    Re-sending from there would append bytes the file already has."""
    source = _file(tmp_path / "clips" / "c.mp4", 200_000)
    storage, worker = _rig(tmp_path, receiver)
    storage.create_run("run-1", label="Level 2")
    storage.enqueue_upload("run-1", "clip", str(source), "clips/c.mp4",
                           source.stat().st_size)

    chunks = [0]
    worker.busy = lambda: (chunks.__setitem__(0, chunks[0] + 1), chunks[0] > 2)[1]
    worker.drain()

    row = storage.list_uploads("run-1")[0]
    storage.update_upload(row["id"], sent_bytes=0)      # the record lags reality

    storage, worker = _rig(tmp_path, receiver)
    worker.drain()

    landed = _stored(receiver, "run-1", "clips/c.mp4")
    assert landed.stat().st_size == source.stat().st_size
    assert _digest(landed) == _digest(source)


def test_nothing_is_uploaded_while_a_walk_is_in_progress(receiver, tmp_path):
    """Uploading over the link under test would manufacture the latency and
    loss the run exists to measure."""
    source = _file(tmp_path / "clips" / "c.mp4", 100_000)
    storage, worker = _rig(tmp_path, receiver, busy=lambda: True)
    storage.create_run("run-1", label="Level 2")
    storage.enqueue_upload("run-1", "clip", str(source), "clips/c.mp4",
                           source.stat().st_size)

    assert worker.drain() == 0
    assert not _stored(receiver, "run-1", "clips/c.mp4.part").exists()
    assert storage.list_uploads("run-1")[0]["state"] == "pending"


def test_the_full_video_waits_until_somebody_asks(receiver, tmp_path):
    """Clips are tens of megabytes and go automatically; the whole walk is
    hundreds and goes only when wanted."""
    clip = _file(tmp_path / "clips" / "c.mp4", 50_000)
    video = _file(tmp_path / "video" / "full.mp4", 400_000)
    storage, worker = _rig(tmp_path, receiver)
    storage.create_run("run-1", label="Level 2")
    storage.enqueue_upload("run-1", "clip", str(clip), "clips/c.mp4", 50_000)
    storage.enqueue_upload("run-1", "video", str(video), "video/full.mp4",
                           400_000, held=True)

    worker.drain()
    assert _stored(receiver, "run-1", "clips/c.mp4").exists()
    assert not _stored(receiver, "run-1", "video/full.mp4").exists()

    # Somebody presses the button on the report's second tab.
    receiver.write_meta("run-1", {"requests": {"full_video": True},
                                  "share_key": "x"})
    worker.collect_requests()
    worker.drain()
    assert _digest(_stored(receiver, "run-1", "video/full.mp4")) == _digest(video)


def test_a_file_deleted_before_it_was_sent_fails_loudly_and_moves_on(receiver, tmp_path):
    """One missing clip must not wedge the queue behind it forever."""
    storage, worker = _rig(tmp_path, receiver)
    storage.create_run("run-1", label="Level 2")
    storage.enqueue_upload("run-1", "clip", str(tmp_path / "gone.mp4"),
                           "clips/gone.mp4", 10)
    good = _file(tmp_path / "clips" / "good.mp4", 40_000)
    storage.enqueue_upload("run-1", "clip", str(good), "clips/good.mp4", 40_000)

    worker.drain()

    states = {u["remote_name"]: u["state"] for u in storage.list_uploads("run-1")}
    assert states["clips/gone.mp4"] == "failed"
    assert states["clips/good.mp4"] == "done"


# ---- who may read a report -------------------------------------------------
#
# Run identifiers are readable by design (20250911-194640-level-2-garage), so a
# report URL is guessable and the contents name a customer's site. "Nobody will
# find it" is not access control.

def _get(url: str, password: str | None = None) -> int:
    import base64
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url)
    if password is not None:
        token = base64.b64encode(f"viabot:{password}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.fixture
def published(receiver):
    """One run on the server, complete with a report and a share key."""
    directory = receiver.DATA_DIR / "runs" / "run-1"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text("<!doctype html><p>Level 2</p>")
    receiver.write_meta("run-1", {"label": "Level 2", "share_key": "sekrit-key"})
    receiver.VIEWER_PASSWORD = "hunter2"
    return receiver


def test_a_report_is_not_readable_without_the_password(published):
    assert _get(f"{published.base_url}/r/run-1/") == 401
    assert _get(f"{published.base_url}/") == 401


def test_the_password_opens_everything(published):
    assert _get(f"{published.base_url}/", "hunter2") == 200
    assert _get(f"{published.base_url}/r/run-1/", "hunter2") == 200


def test_a_share_key_opens_exactly_one_report(published):
    """So a customer can be sent one survey without being handed the archive."""
    assert _get(f"{published.base_url}/r/run-1/?k=sekrit-key") == 200
    assert _get(f"{published.base_url}/r/run-1/?k=wrong") == 401
    assert _get(f"{published.base_url}/?k=sekrit-key") == 401


def test_a_share_key_does_not_open_a_different_run(published):
    other = published.DATA_DIR / "runs" / "run-2"
    other.mkdir(parents=True, exist_ok=True)
    (other / "index.html").write_text("<p>somebody else's site</p>")
    published.write_meta("run-2", {"share_key": "other-key"})
    assert _get(f"{published.base_url}/r/run-2/?k=sekrit-key") == 401


def test_a_run_that_has_not_finished_uploading_says_so(published):
    published.write_meta("run-3", {"label": "Level 3", "share_key": "k3"})
    (published.DATA_DIR / "runs" / "run-3").mkdir(parents=True, exist_ok=True)
    assert _get(f"{published.base_url}/r/run-3/?k=k3") == 202
