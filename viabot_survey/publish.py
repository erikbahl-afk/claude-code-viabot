"""Send a finished run somewhere a person can open it.

The hard requirement here is not the uploading, it is the interrupting. This
rig runs off a battery through a screw terminal and has already lost power
mid-operation more than once. A customer may well switch it off the moment a
walk ends — before the upload starts, or halfway through a 60 MB clip. Either
way the next power-on has to carry on rather than start again, over a cellular
link that is the very thing being measured.

So the protocol is deliberately dull: ask the far end how many bytes it already
has, then send the rest in pieces. Two rules make it survive a cut at any
instant.

**The receiver is the authority on progress.** The rig records how far it has
got, but only as a progress bar. Power can be cut between a chunk landing on
the server and the rig learning that it did, so a resume always starts by
asking rather than by trusting the local number.

**A chunk is appended at a stated offset or not at all.** The receiver refuses
anything that does not continue exactly where it left off, which makes a
duplicated or reordered chunk harmless instead of corrupting.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

#: Bytes per request. Small enough that a cut costs little and a bad cell does
#: not time out mid-chunk; large enough that the per-request overhead stays
#: irrelevant. Every chunk boundary is a place the transfer can safely stop.
DEFAULT_CHUNK_BYTES = 512 * 1024

API = "/api/v1"


class PublishError(RuntimeError):
    """Anything that went wrong talking to the receiver."""


class OffsetConflict(PublishError):
    """The receiver has a different amount than we assumed.

    Not really an error: it is what a resume looks like when the local record
    was stale. The caller re-reads the offset and carries on from there.
    """

    def __init__(self, offset: int) -> None:
        super().__init__(f"receiver is at offset {offset}")
        self.offset = offset


class PublishClient:
    """Talks to the receiving server. One instance, reused."""

    def __init__(self, base_url: str, token: str = "",
                 chunk_bytes: int = DEFAULT_CHUNK_BYTES,
                 timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.chunk_bytes = max(64 * 1024, int(chunk_bytes))
        self.timeout = float(timeout)

    # -- plumbing ------------------------------------------------------------

    def _request(self, method: str, path: str, data: bytes | None = None,
                 headers: dict[str, str] | None = None) -> tuple[int, dict, bytes]:
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, method=method)
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()[:300]
            if exc.code == 409:
                raise OffsetConflict(_int(dict(exc.headers).get("Upload-Offset"))) from exc
            if exc.code == 404:
                return 404, dict(exc.headers), body
            raise PublishError(f"{method} {path}: HTTP {exc.code} {body!r}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise PublishError(f"{method} {path}: {exc}") from exc

    # -- the protocol --------------------------------------------------------

    def remote_offset(self, run_id: str, name: str) -> tuple[int, bool]:
        """How many bytes the receiver holds, and whether it considers it done.

        A file it has never heard of is offset zero, not an error: that is the
        normal state of every upload before the first chunk.
        """
        status, headers, _ = self._request("HEAD", f"{API}/runs/{run_id}/files/{name}")
        if status == 404:
            return 0, False
        return _int(headers.get("Upload-Offset")), headers.get("Upload-Complete") == "1"

    def send_chunk(self, run_id: str, name: str, offset: int, total: int,
                   payload: bytes) -> int:
        """Append one chunk. Returns the receiver's new offset."""
        _, headers, _ = self._request(
            "PATCH", f"{API}/runs/{run_id}/files/{name}", data=payload,
            headers={
                "Content-Type": "application/octet-stream",
                "Upload-Offset": str(offset),
                "Upload-Length": str(total),
            })
        return _int(headers.get("Upload-Offset"), offset + len(payload))

    def upload(self, run_id: str, name: str, path: Path,
               on_progress: Callable[[int], None] | None = None,
               should_stop: Callable[[], bool] | None = None) -> int:
        """Send a file, starting from wherever the receiver already is.

        ``on_progress`` is called with the confirmed offset after every chunk,
        and is how the queue records progress durably. ``should_stop`` lets a
        starting survey interrupt a large upload between chunks — measuring the
        link while saturating it would poison the very numbers being collected.
        """
        path = Path(path)
        if not path.exists():
            raise PublishError(f"{path} is gone")
        total = path.stat().st_size

        offset, complete = self.remote_offset(run_id, name)
        if complete or offset >= total:
            return total

        with path.open("rb") as handle:
            handle.seek(offset)
            while offset < total:
                if should_stop and should_stop():
                    return offset
                payload = handle.read(self.chunk_bytes)
                if not payload:
                    break
                try:
                    offset = self.send_chunk(run_id, name, offset, total, payload)
                except OffsetConflict as conflict:
                    # The receiver kept more (or less) than we thought, which is
                    # exactly what a resume after a badly timed power cut looks
                    # like. Believe it and re-seek.
                    log.info("resuming %s from the receiver's offset %d",
                             name, conflict.offset)
                    offset = conflict.offset
                    handle.seek(offset)
                    continue
                handle.seek(offset)
                if on_progress:
                    on_progress(offset)
        return offset

    # -- run metadata --------------------------------------------------------

    def put_manifest(self, run_id: str, manifest: dict[str, Any]) -> None:
        """Tell the receiver what this run is, so it can list it by name."""
        self._request("POST", f"{API}/runs/{run_id}/manifest",
                      data=json.dumps(manifest).encode(),
                      headers={"Content-Type": "application/json"})

    def pending_requests(self) -> dict[str, Any]:
        """Everything anyone has asked for, across every run, in one call.

        The rig sits behind carrier NAT with no inbound route, so nobody can
        push a request to it; it has to ask. Asking once for everything rather
        than once per run keeps that cost flat as the number of past surveys
        grows.
        """
        status, _, body = self._request("GET", f"{API}/requests")
        if status == 404:
            return {}
        try:
            return json.loads(body or b"{}")
        except ValueError:
            return {}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default
