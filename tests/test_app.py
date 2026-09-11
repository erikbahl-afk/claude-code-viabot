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
    body = client.post("/api/run/start", json={}).get_json()
    assert "changeme123" not in str(body)
    assert '"password": "********"' in body["run"]["config_json"]
    stored = storage.get_run(body["run"]["id"])["config_json"]
    assert "changeme123" not in stored
    client.post("/api/run/stop")


# ---- run lifecycle ---------------------------------------------------------

def test_run_start_stop_and_mark(client):
    started = client.post("/api/run/start", json={"label": "Sunset L2"})
    assert started.status_code == 200
    run_id = started.get_json()["run"]["id"]
    assert run_id.endswith("-sunset-l2")

    assert client.post("/api/run/start", json={}).status_code == 409

    marked = client.post("/api/mark", json={"category": "Ramp", "note": "L1 to L2"})
    assert marked.status_code == 200
    assert marked.get_json()["mark"]["category"] == "Ramp"

    assert len(client.get("/api/marks").get_json()["marks"]) == 1
    assert client.post("/api/run/stop").status_code == 200
    assert client.post("/api/run/stop").status_code == 409


def test_marks_are_refused_when_no_run_is_recording(client):
    assert client.post("/api/mark", json={"category": "Ramp"}).status_code == 409


def test_iperf_endpoint_explains_itself_when_disabled(client):
    response = client.post("/api/iperf/test")
    assert response.status_code == 409
    assert "IPERF_SERVER.md" in response.get_json()["error"]


def test_status_payload_has_what_the_dashboard_needs(client):
    body = client.get("/api/status").get_json()
    assert {"sample", "run", "workers", "system", "version"} <= set(body)
    assert {"ping", "dns", "iperf3", "router", "camera"} <= set(body["workers"])


def test_active_run_cannot_be_deleted(client):
    run_id = client.post("/api/run/start", json={}).get_json()["run"]["id"]
    assert client.delete(f"/api/runs/{run_id}").status_code == 409
    client.post("/api/run/stop")
    assert client.delete(f"/api/runs/{run_id}").status_code == 200


def test_update_is_refused_mid_run(client):
    client.post("/api/run/start", json={})
    response = client.post("/api/update/apply")
    assert response.status_code == 409
    assert "stop the run" in response.get_json()["error"]
    client.post("/api/run/stop")


# ---- export and reporting --------------------------------------------------

def test_samples_csv_exports_the_video_pointer(client, storage):
    run_id = client.post("/api/run/start", json={}).get_json()["run"]["id"]
    now = time.time()
    storage.add_sample(run_id, now, rtt_ms=50.0, loss_pct=0.0, status="good",
                       video_file="20260911-140000.mkv", video_offset_s=12.0)
    client.post("/api/run/stop")

    response = client.get(f"/api/runs/{run_id}/samples.csv")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    header, first = text.strip().splitlines()[:2]
    assert "video_file" in header and "iso_time" in header
    assert "20260911-140000.mkv" in first


def test_csv_export_of_an_unknown_run_404s(client):
    assert client.get("/api/runs/nope/samples.csv").status_code == 404


def test_report_groups_contiguous_bad_samples_into_problem_areas(storage):
    """The report exists to answer one question: which stretches were bad, and
    what was the camera pointed at during them."""
    run = storage.create_run("r1")
    base = 1_700_000_000.0
    statuses = (["good"] * 5 + ["bad"] * 3 + ["dead"] * 4 + ["good"] * 5
                + ["bad"] * 2 + ["good"] * 3)
    for offset, status in enumerate(statuses):
        storage.add_sample("r1", base + offset, rtt_ms=None if status == "dead" else 50.0,
                           loss_pct=100.0 if status == "dead" else 0.0, status=status,
                           video_file="20260911-140000.mkv", video_offset_s=float(offset))
    storage.add_mark("r1", base + 9, category="Ramp", note="L1 to L2")
    storage.end_run("r1")

    report = build_report(storage, storage.get_run("r1"))
    areas = report["problem_areas"]
    assert len(areas) == 2
    assert areas[0]["worst_status"] == "dead"     # escalates within one stretch
    assert areas[0]["duration_s"] == 7.0          # the bad and dead stretches merge into one
    assert areas[0]["video_file"] == "20260911-140000.mkv"
    assert [m["category"] for m in areas[0]["marks"]] == ["Ramp"]
    assert areas[1]["worst_status"] == "bad"
    assert areas[1]["marks"] == []
    assert report["status_pct"]["good"] == 59.1


def test_report_of_a_clean_walk_has_no_problem_areas(storage):
    storage.create_run("clean")
    for offset in range(10):
        storage.add_sample("clean", 1_700_000_000.0 + offset, rtt_ms=45.0,
                           loss_pct=0.0, status="good")
    report = build_report(storage, storage.get_run("clean"))
    assert report["problem_areas"] == []
    assert report["rtt_ms"]["avg"] == 45.0


def test_health_endpoint_is_cheap_and_always_answers(client):
    body = client.get("/api/health").get_json()
    assert body["ok"] is True
