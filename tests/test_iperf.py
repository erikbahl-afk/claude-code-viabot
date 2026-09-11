import json

from viabot_survey.workers.iperf import Iperf3Worker, parse_iperf3_json


def test_parses_a_successful_run():
    payload = json.dumps({"end": {
        "sum_received": {"bits_per_second": 43_500_000.0, "bytes": 27_187_500},
        "sum_sent": {"bits_per_second": 44_000_000.0, "bytes": 27_500_000, "retransmits": 4},
    }})
    result = parse_iperf3_json(payload)
    assert result["error"] is None
    assert result["mbps"] == 43.5          # receiver-side, not the flattering figure
    assert result["bytes"] == 27_187_500
    assert result["retransmits"] == 4


def test_surfaces_an_iperf_error():
    result = parse_iperf3_json(json.dumps({"error": "unable to connect to server"}))
    assert "unable to connect" in result["error"]


def test_handles_no_output_at_all():
    assert "no output" in parse_iperf3_json("", "")["error"]


def test_handles_non_json_output():
    result = parse_iperf3_json("iperf3: error - the server is busy", "")
    assert "server is busy" in result["error"]


def test_worker_stays_disabled_without_a_server():
    """The only thing that spends cellular data must not start by accident."""
    assert Iperf3Worker(server=None, enabled=True).enabled is False
    assert Iperf3Worker(server="10.0.0.1", enabled=False).enabled is False
    assert Iperf3Worker(server="10.0.0.1", enabled=True).enabled is True


def test_command_applies_the_bitrate_cap_and_direction():
    worker = Iperf3Worker(server="10.0.0.1", bitrate="25M", duration_s=5, enabled=True)
    download = " ".join(worker.build_command(reverse=True))
    assert "-b 25M" in download and "-R" in download and "-t 5" in download
    assert "-R" not in " ".join(worker.build_command(reverse=False))


def test_budget_stops_further_tests(monkeypatch):
    events = []
    worker = Iperf3Worker(server="10.0.0.1", enabled=True, run_data_budget_mb=10,
                          on_event=lambda level, msg: events.append(msg))
    assert worker._budget_allows() is True
    worker._bytes_this_run = 11_000_000
    assert worker._budget_allows() is False
    assert any("budget" in message for message in events)
    worker.reset_run_budget()
    assert worker._budget_allows() is True


def test_no_budget_means_no_ceiling():
    worker = Iperf3Worker(server="10.0.0.1", enabled=True, run_data_budget_mb=None)
    worker._bytes_this_run = 10**12
    assert worker._budget_allows() is True
    assert worker.budget_remaining_mb is None
