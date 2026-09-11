import re
import time

import pytest

from viabot_survey.app import CAPTIVE_PROBE_PATHS, build_report
from viabot_survey.config import redact


# ---- captive portal --------------------------------------------------------

@pytest.mark.parametrize("host,path", [
    ("connectivitycheck.gstatic.com", "/generate_204"),   # Android
    ("captive.apple.com", "/hotspot-detect.html"),        # iOS / macOS
    ("www.msftconnecttest.com", "/connecttest.txt"),      # Windows
    ("detectportal.firefox.com", "/success.txt"),         # Firefox
    ("example.com", "/anything-at-all"),                  # stray browser tab
])
def test_connectivity_probes_are_redirected_to_the_dashboard(client, config, host, path):
    response = client.get(path, headers={"Host": host})
    assert response.status_code == 302
    assert response.headers["Location"] == f"http://{config['ap']['address']}/"
    # Some captive-portal agents render the body instead of following the
    # redirect, so it has to carry a link too.
    assert config["ap"]["address"] in response.get_data(as_text=True)


@pytest.mark.parametrize("path", CAPTIVE_PROBE_PATHS)
def test_probes_sent_straight_to_our_own_address_also_redirect(client, path):
    """A phone that already has our IP still probes; answering 204 there would
    convince it the network has internet and the portal would never open."""
    assert client.get(path).status_code == 302


def test_the_dashboard_itself_is_not_redirected(client):
    assert client.get("/").status_code == 200


def test_api_404s_stay_json(client):
    response = client.get("/api/nope")
    assert response.status_code == 404
    assert response.get_json()["error"] == "not found"


# ---- secrets ---------------------------------------------------------------

def test_redact_masks_secrets_at_any_depth():
    cleaned = redact({"ap": {"ssid": "X", "password": "hunter2"},
                      "router": {"password": "s3cret", "username": "root"},
                      "list": [{"token": "abc"}]})
    assert cleaned["ap"]["password"] == "********"
    assert cleaned["router"]["password"] == "********"
    assert cleaned["list"][0]["token"] == "********"
    assert cleaned["ap"]["ssid"] == "X"
    assert cleaned["router"]["username"] == "root"


def test_config_endpoint_never_leaks_the_wifi_password(client):
    body = client.get("/api/config").get_json()
    assert body["ap"]["password"] == "********"
    assert "changeme123" not in str(body)


def test_starting_a_run_never_returns_or_stores_the_wifi_password(client, storage):
    """The run record embeds the config in force, is handed straight back over
    the API, and the database gets copied onto laptops."""
    body = client.post("/api/run/start", json={"label": "Sunset L2"}).get_json()
    assert "changeme123" not in str(body)
    assert '"password": "********"' in body["run"]["config_json"]
    stored = storage.get_run(body["run"]["id"])["config_json"]
    assert "changeme123" not in stored
    client.post("/api/run/stop")


# ---- run lifecycle ---------------------------------------------------------

def test_run_start_pause_resume_and_stop(client):
    started = client.post("/api/run/start", json={"label": "Sunset L2"})
    assert started.status_code == 200
    assert started.get_json()["run"]["id"].endswith("-sunset-l2")

    assert client.post("/api/run/start", json={"label": "Other"}).status_code == 409

    assert client.post("/api/run/pause").status_code == 200
    assert client.get("/api/status").get_json()["paused"] is True
    # Pausing twice is a no-op, not a crash.
    assert client.post("/api/run/pause").status_code == 409

    assert client.post("/api/run/resume").status_code == 200
    assert client.get("/api/status").get_json()["paused"] is False
    assert client.post("/api/run/resume").status_code == 409

    assert client.post("/api/run/stop").status_code == 200
    assert client.post("/api/run/stop").status_code == 409


def test_a_run_cannot_start_without_a_location_name(client):
    """An unlabelled run is just a timestamp; a week later nobody knows which
    garage it was."""
    for label in ("", "   ", "!!!"):
        response = client.post("/api/run/start", json={"label": label})
        assert response.status_code == 400
        assert "location name" in response.get_json()["error"]


def test_pause_and_resume_are_refused_with_no_run(client):
    assert client.post("/api/run/pause").status_code == 409
    assert client.post("/api/run/resume").status_code == 409


def test_iperf_endpoint_explains_itself_when_disabled(client):
    response = client.post("/api/iperf/test")
    assert response.status_code == 409
    assert "IPERF_SERVER.md" in response.get_json()["error"]


def test_status_payload_has_what_the_dashboard_needs(client):
    body = client.get("/api/status").get_json()
    assert {"sample", "run", "workers", "system", "version", "health"} <= set(body)
    assert {"ping", "dns", "iperf3", "router", "camera"} <= set(body["workers"])
    # Health is what the operator sees without opening anything, so every
    # subsystem that can silently ruin a walk must be in it.
    assert {"camera", "link", "power", "disk", "clock"} <= set(body["health"])
    for item in body["health"].values():
        assert "state" in item and "detail" in item


def test_active_run_cannot_be_deleted(client):
    run_id = client.post("/api/run/start", json={"label": "L2"}).get_json()["run"]["id"]
    assert client.delete(f"/api/runs/{run_id}").status_code == 409
    client.post("/api/run/stop")
    assert client.delete(f"/api/runs/{run_id}").status_code == 200


def test_update_is_refused_mid_run(client):
    client.post("/api/run/start", json={"label": "L2"})
    response = client.post("/api/update/apply")
    assert response.status_code == 409
    assert "stop the run" in response.get_json()["error"]
    client.post("/api/run/stop")


# ---- export and reporting --------------------------------------------------

def test_samples_csv_exports_the_video_pointer(client, storage):
    run_id = client.post("/api/run/start", json={"label": "L2"}).get_json()["run"]["id"]
    now = time.time()
    storage.add_sample(run_id, now, rtt_ms=50.0, loss_pct=0.0, status="good",
                       video_file="20260911-140000.mkv", video_offset_s=12.0)
    client.post("/api/run/stop")

    response = client.get(f"/api/runs/{run_id}/samples.csv")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    header, first = text.strip().splitlines()[:2]
    assert "video_file" in header
    # Both clocks, explicitly: video segments are named in UTC, but the
    # operator reads local time. A bare timestamp would be ambiguous.
    assert "iso_utc" in header and "iso_local" in header
    assert "undervoltage" in header
    assert "20260911-140000.mkv" in first


def test_exported_timestamps_carry_their_zone(client, storage):
    run_id = client.post("/api/run/start", json={"label": "L2"}).get_json()["run"]["id"]
    storage.add_sample(run_id, 1_700_000_000.0, rtt_ms=50.0, status="good")
    client.post("/api/run/stop")
    rows = client.get(f"/api/runs/{run_id}/samples.csv").get_data(as_text=True)
    header, first = rows.strip().splitlines()[:2]
    fields = dict(zip(header.split(","), first.split(",")))
    assert fields["iso_utc"] == "2023-11-14T22:13:20Z"
    # Local column must state its offset rather than leaving it to be guessed.
    assert re.search(r"[+-]\d{4}$", fields["iso_local"])


def test_csv_export_of_an_unknown_run_404s(client):
    assert client.get("/api/runs/nope/samples.csv").status_code == 404


def test_report_carries_the_dead_zones_and_the_headline_percentage(storage):
    """The report answers one question: how much of the walk was usable, and
    where was it not."""
    storage.create_run("r1")
    base = 1_700_000_000.0
    for offset in range(100):
        dead = 20 <= offset < 40
        storage.add_sample("r1", base + offset,
                           rtt_ms=None if dead else 50.0,
                           loss_pct=100.0 if dead else 0.0,
                           status="dead" if dead else "good",
                           video_file="20231114T221320Z.mkv",
                           video_offset_s=float(offset))
    storage.replace_dead_zones("r1", [{
        "index": 1, "start_ts": base + 20, "end_ts": base + 39, "duration_s": 20.0,
        "worst_loss_pct": 100.0, "sample_count": 20,
        "video_file": "20231114T221320Z.mkv", "video_offset_s": 20.0,
        "clip_path": "/data/clips/r1/deadzone-01.mp4",
    }])
    storage.set_run_summary("r1", {"runnable_pct": 80.0, "walked_s": 100.0,
                                   "dead_s": 20.0, "dead_zone_count": 1})
    storage.end_run("r1")

    report = build_report(storage, storage.get_run("r1"))
    assert report["runnable_pct"] == 80.0
    assert len(report["dead_zones"]) == 1
    zone = report["dead_zones"][0]
    assert zone["duration_s"] == 20.0
    assert zone["clip_path"].endswith(".mp4")
    # Both clocks, so the entry is unambiguous in an exported file.
    assert zone["start_utc"].endswith("Z")
    assert re.search(r"[+-]\d{4}$", zone["start_local"])


def test_report_of_a_clean_walk_has_no_dead_zones(storage):
    storage.create_run("clean")
    for offset in range(10):
        storage.add_sample("clean", 1_700_000_000.0 + offset, rtt_ms=45.0,
                           loss_pct=0.0, status="good")
    report = build_report(storage, storage.get_run("clean"))
    assert report["dead_zones"] == []
    assert report["status_pct"]["good"] == 100.0


def test_dead_zone_csv_export(client, storage):
    run_id = client.post("/api/run/start", json={"label": "L2"}).get_json()["run"]["id"]
    client.post("/api/run/stop")
    storage.replace_dead_zones(run_id, [{
        "index": 1, "start_ts": 1_700_000_000.0, "end_ts": 1_700_000_019.0,
        "duration_s": 20.0, "worst_loss_pct": 100.0, "sample_count": 20,
        "clip_path": "/data/clips/x.mp4",
    }])
    text = client.get(f"/api/runs/{run_id}/deadzones.csv").get_data(as_text=True)
    header, first = text.strip().splitlines()[:2]
    assert "clip_path" in header and "start_utc" in header
    assert "/data/clips/x.mp4" in first


def test_health_endpoint_is_cheap_and_always_answers(client):
    body = client.get("/api/health").get_json()
    assert body["ok"] is True
