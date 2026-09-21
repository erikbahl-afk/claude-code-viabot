"""The UDP load test, in both directions.

The interval lines in here are verbatim iperf3 output captured from a real
client-server pair, not written by hand. Output formats are the thing a parser
gets quietly wrong, and a fixture invented alongside the regex only proves the
two agree with each other.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time

import pytest

from viabot_survey.workers import udpload
from viabot_survey.workers.udpload import (DOWNLINK, UPLINK, UdpLoadWorker,
                                           parse_interval, parse_server_output)

# Real iperf3 3.16 output, UDP, 1200-byte datagrams.
RECEIVER_LINE = "[  5]   0.00-1.00   sec   123 KBytes  1.01 Mbits/sec  0.062 ms  0/105 (0%)  "
LOSSY_LINE = "[  5]   1.00-2.00   sec   122 KBytes   998 Kbits/sec  0.027 ms  3/104 (2.9%)  "
SENDER_LINE = "[  5]   0.00-1.00   sec   123 KBytes  1.01 Mbits/sec  105  "
SUMMARY = "[  5]   0.00-6.00   sec   732 KBytes  1.00 Mbits/sec  0.030 ms  0/625 (0%)  receiver"
HEADER = "[ ID] Interval           Transfer     Bitrate         Jitter    Lost/Total Datagrams"

# What --get-server-output relays back after an uplink block: the far end's own
# per-second view, which is the only place uplink loss exists.
SERVER_OUTPUT = """Connecting to host 10.0.0.1, port 5201
[  5]   0.00-1.00   sec   123 KBytes  1.01 Mbits/sec  105
[  5]   1.00-2.00   sec   122 KBytes   998 Kbits/sec  104

Server output:
Accepted connection from 203.0.113.7, port 40040
[  5] local 10.0.0.1 port 5201 connected to 203.0.113.7 port 44991
[ ID] Interval           Transfer     Bitrate         Jitter    Lost/Total Datagrams
[  5]   0.00-1.00   sec   123 KBytes  1.01 Mbits/sec  0.075 ms  0/105 (0%)
[  5]   1.00-2.00   sec   100 KBytes   820 Kbits/sec  8.244 ms  19/104 (18%)
- - - - - - - - - - - - - - - - - - - - - - - - -
[ ID] Interval           Transfer     Bitrate         Jitter    Lost/Total Datagrams
[  5]   0.00-2.00   sec   223 KBytes   915 Kbits/sec  4.018 ms  19/209 (9.1%)  receiver
"""


# ---- parsing ---------------------------------------------------------------

def test_a_receiving_line_carries_jitter_and_loss():
    reading = parse_interval(RECEIVER_LINE)
    assert reading["jitter_ms"] == 0.062
    assert reading["loss_pct"] == 0.0
    assert round(reading["mbps"], 2) == 1.01


def test_a_sending_line_carries_neither():
    """This is the whole reason uplink is measured differently: the end doing
    the sending cannot see what arrived."""
    reading = parse_interval(SENDER_LINE)
    assert reading["jitter_ms"] is None
    assert reading["loss_pct"] is None
    assert reading["total"] == 105


def test_kilobit_rates_are_scaled_like_megabit_ones():
    """iperf3 switches units on its own once a link degrades, and a bad garage
    is exactly where it starts printing Kbits/sec."""
    assert round(parse_interval(LOSSY_LINE)["mbps"], 3) == 0.998
    assert parse_interval(LOSSY_LINE)["loss_pct"] == 2.9


def test_summary_lines_are_not_mistaken_for_readings():
    """They have the identical shape and cover the whole test, so counting one
    would put a run-length average onto a single second."""
    assert parse_interval(SUMMARY) is None
    assert parse_interval(HEADER) is None
    assert parse_interval("iperf Done.") is None
    assert parse_interval("") is None


def test_the_far_ends_readings_are_recovered_from_a_block():
    readings = parse_server_output(SERVER_OUTPUT)
    assert len(readings) == 2
    assert readings[1]["loss_pct"] == 18.0
    assert readings[1]["jitter_ms"] == 8.244


def test_the_rigs_own_lines_are_not_counted_as_the_far_ends():
    """Both appear in the same captured output. Reading the rig's own would
    report what it sent as though it had arrived — which is exactly the error
    that makes a garage look better than it is."""
    readings = parse_server_output(SERVER_OUTPUT)
    assert all(r["loss_pct"] is not None for r in readings)
    assert parse_server_output("no server section here") == []


# ---- the commands ----------------------------------------------------------

def test_uplink_runs_in_blocks_and_asks_what_arrived():
    argv = UdpLoadWorker("10.0.0.1", UPLINK, block_s=30).build_command()
    assert "-u" in argv and "-R" not in argv          # the rig sends
    assert "--get-server-output" in argv              # ...so ask the far end
    assert argv[argv.index("-t") + 1] == "30"


def test_downlink_streams_for_the_whole_walk():
    argv = UdpLoadWorker("10.0.0.1", DOWNLINK).build_command()
    assert "-R" in argv                               # the server sends
    assert argv[argv.index("-t") + 1] == "0"          # no end
    assert "--get-server-output" not in argv          # the rig can see it itself


def test_both_directions_ask_for_rtp_sized_datagrams():
    """iperf3 defaults to 32 KB datagrams, which IP fragments into two dozen
    packets. Losing any one marks the whole datagram lost, so loss reads several
    times worse than a real video packet would see."""
    for direction in (UPLINK, DOWNLINK):
        argv = UdpLoadWorker("10.0.0.1", direction).build_command()
        assert argv[argv.index("-l") + 1] == "1200"
        assert "--forceflush" in argv


def test_the_two_directions_take_different_ports():
    """One iperf3 server runs one test at a time, so they cannot share one."""
    up = UdpLoadWorker("10.0.0.1", UPLINK, port=5201)
    down = UdpLoadWorker("10.0.0.1", DOWNLINK, port=5202)
    assert up.name != down.name
    assert up.build_command()[4] != down.build_command()[4]


# ---- authentication --------------------------------------------------------

def test_the_password_never_reaches_the_command_line():
    """A walk runs this for half an hour; argv is readable from ps throughout."""
    worker = UdpLoadWorker("10.0.0.1", UPLINK, username="rig", password="hunter2",
                           public_key_path="/etc/viabot/iperf.pub")
    assert not any("hunter2" in arg for arg in worker.build_command())
    assert worker.build_env()["IPERF3_PASSWORD"] == "hunter2"


def test_credentials_are_only_sent_when_both_halves_are_configured():
    argv = UdpLoadWorker("10.0.0.1", UPLINK, username="rig").build_command()
    assert "--username" not in argv
    assert UdpLoadWorker("10.0.0.1", UPLINK, username="rig").build_env() is None


# ---- the data ceiling ------------------------------------------------------

def test_the_run_budget_is_a_hard_stop():
    worker = UdpLoadWorker("10.0.0.1", UPLINK, run_data_budget_mb=1)
    assert not worker.budget_spent
    for _ in range(10):
        worker._consume("[  5] 0.00-1.00 sec 200 KBytes 1.64 Mbits/sec 170")
    assert worker.budget_spent


def test_each_walk_gets_a_fresh_allowance():
    worker = UdpLoadWorker("10.0.0.1", UPLINK, run_data_budget_mb=1)
    for _ in range(10):
        worker._consume("[  5] 0.00-1.00 sec 200 KBytes 1.64 Mbits/sec 170")
    worker.begin_run()
    assert not worker.budget_spent


def test_no_budget_means_no_ceiling():
    assert not UdpLoadWorker("10.0.0.1", UPLINK, run_data_budget_mb=None).budget_spent


# ---- what reaches a sample -------------------------------------------------

def test_downlink_readings_are_named_for_their_direction():
    worker = UdpLoadWorker("10.0.0.1", DOWNLINK)
    worker._latest = parse_interval(RECEIVER_LINE)
    worker._latest_ts = time.time()
    fields = worker.sample_fields()
    assert fields["udp_down_loss_pct"] == 0.0
    assert "udp_up_loss_pct" not in fields


def test_a_stale_reading_is_dropped_rather_than_repeated():
    """When the link dies badly the iperf3 control channel — which is TCP —
    dies with it and the stream stops. Carrying the last good number forward
    would paint the worst spot in the garage as the healthiest."""
    worker = UdpLoadWorker("10.0.0.1", DOWNLINK)
    worker._latest = parse_interval(RECEIVER_LINE)
    worker._latest_ts = time.time() - 30
    assert worker.sample_fields()["udp_down_loss_pct"] is None


def test_uplink_never_reports_through_the_live_path():
    """Its numbers do not exist at the rig until a block ends, so they are
    written onto their samples afterwards instead."""
    worker = UdpLoadWorker("10.0.0.1", UPLINK)
    worker._latest = parse_interval(RECEIVER_LINE)
    worker._latest_ts = time.time()
    assert worker.sample_fields() == {"udp_up_jitter_ms": None,
                                      "udp_up_loss_pct": None,
                                      "udp_up_mbps": None}


def test_a_finished_block_is_placed_on_the_seconds_it_covers():
    """The far end reports elapsed offsets, which know nothing about the time
    spent setting the test up. The wall clock recorded as each of the rig's own
    lines arrived is what puts the readings on the right samples."""
    collected = []
    worker = UdpLoadWorker("10.0.0.1", UPLINK, on_backfill=collected.extend)
    now = time.time()
    worker._absorb_server_output(SERVER_OUTPUT, started=now - 5,
                                 interval_ends=[now - 1.0, now])

    assert [round(ts - now, 1) for ts, _ in collected] == [-1.5, -0.5]
    assert collected[1][1]["udp_up_loss_pct"] == 18.0
    assert collected[1][1]["udp_up_jitter_ms"] == 8.244


def test_a_block_the_server_said_nothing_about_degrades_rather_than_invents():
    worker = UdpLoadWorker("10.0.0.1", UPLINK, on_backfill=lambda r: None)
    worker._absorb_server_output("no server section", started=time.time(),
                                 interval_ends=[])
    assert worker.state == "degraded"


def test_a_worker_with_no_server_configured_stays_off():
    assert not UdpLoadWorker(None, UPLINK).enabled


# ---- when it is allowed to run ---------------------------------------------
#
# The stream is a deliberate, continuous load on the uplink. Left running
# between walks it would burn the link for nothing, and fight the publisher for
# the same uplink while that is trying to send the last run's clips.

@pytest.fixture
def loaded_runner(config, storage):
    """A runner with both directions switched on, pointed at an address that
    does not answer. Whether iperf3 connects is beside the point here — the
    question is only whether the rig lets the workers run at all."""
    from viabot_survey.runner import SurveyRunner

    config._data["udp_load"]["enabled"] = True
    config._data["udp_load"]["server"] = "192.0.2.1"     # TEST-NET-1, unroutable
    survey = SurveyRunner(config, storage)
    survey.start()
    yield survey
    survey.shutdown()


def _streaming(worker) -> bool:
    thread = worker._thread
    return thread is not None and thread.is_alive()


def test_neither_direction_runs_between_walks(loaded_runner):
    assert all(w.enabled for w in loaded_runner.udp_load)
    assert not any(_streaming(w) for w in loaded_runner.udp_load)


def test_both_directions_run_for_the_length_of_a_walk(loaded_runner):
    loaded_runner.start_run(label="Level 2")
    assert all(_streaming(w) for w in loaded_runner.udp_load)

    loaded_runner.stop_run()
    assert not any(_streaming(w) for w in loaded_runner.udp_load)


def test_pausing_stops_the_load_too(loaded_runner):
    """Pause means this time did not happen. Streaming while the operator
    stands still spends the uplink on the one period it tells you nothing
    about, and leaves data on seconds the report says never happened."""
    loaded_runner.start_run(label="Level 2")
    loaded_runner.pause()
    assert not any(_streaming(w) for w in loaded_runner.udp_load)

    loaded_runner.resume()
    assert all(_streaming(w) for w in loaded_runner.udp_load)


def test_a_block_landing_after_a_pause_is_discarded(loaded_runner):
    """A block in flight when the operator pauses describes seconds the report
    says did not happen."""
    loaded_runner.start_run(label="Level 2")
    run_id = loaded_runner.active_run_id
    loaded_runner.pause()

    loaded_runner._backfill_uplink([(time.time(), {"udp_up_loss_pct": 50.0})])
    assert not any(s["udp_up_loss_pct"] is not None
                   for s in loaded_runner.storage.iter_samples(run_id))


# ---- against a real iperf3, when there is one ------------------------------

requires_iperf3 = pytest.mark.skipif(
    shutil.which("iperf3") is None, reason="iperf3 is not installed")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve(port: int) -> subprocess.Popen:
    proc = subprocess.Popen(["iperf3", "-s", "-p", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    return proc


def _wait_for(predicate, seconds: float = 30) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return False


@requires_iperf3
def test_downlink_readings_flow_from_a_real_server():
    """The flags are what is most likely to be subtly wrong — a missing
    --forceflush produces nothing for the whole walk and then everything at
    once, which no parser test would notice."""
    port = _free_port()
    server = _serve(port)
    worker = UdpLoadWorker("127.0.0.1", DOWNLINK, port=port, bitrate="1M",
                           run_data_budget_mb=None)
    try:
        worker.start()
        assert _wait_for(
            lambda: worker.sample_fields()["udp_down_loss_pct"] is not None), \
            f"no reading arrived; last line: {worker._last_line!r}"
        fields = worker.sample_fields()
        assert fields["udp_down_jitter_ms"] >= 0
        assert fields["udp_down_mbps"] > 0
    finally:
        worker.stop()
        server.terminate()
        server.wait(timeout=5)


@requires_iperf3
def test_uplink_loss_comes_back_from_the_far_end_and_lands_on_samples():
    """The novel half: the rig cannot see uplink loss, so it runs a block and
    asks. This exercises the whole path — block, server output, per-second
    readings, timestamps — against the real binary."""
    port = _free_port()
    server = _serve(port)
    collected: list = []
    worker = UdpLoadWorker("127.0.0.1", UPLINK, port=port, bitrate="1M",
                           block_s=5, run_data_budget_mb=None,
                           on_backfill=collected.extend)
    started = time.time()
    try:
        worker.start()
        assert _wait_for(lambda: len(collected) >= 3, seconds=40), \
            f"no block came back; last line: {worker._last_line!r}"
    finally:
        worker.stop()
        server.terminate()
        server.wait(timeout=5)

    # Per-second readings, each carrying what the far end actually received,
    # stamped inside the window the block ran in.
    for ts, fields in collected[:3]:
        assert started <= ts <= time.time()
        assert fields["udp_up_loss_pct"] is not None
        assert fields["udp_up_jitter_ms"] is not None
    assert worker.snapshot()["backfilled_samples"] >= 3


# ---- binding to the link that is actually being measured -------------------

def test_an_address_is_picked_out_of_ip_output():
    """iperf3's -B takes an address where ping's -I takes a name, so the
    interface has to be resolved before the test can be pinned to it."""
    assert udpload.address_of(
        "3: eth0    inet 192.168.1.42/24 brd 192.168.1.255 scope global eth0"
    ) == "192.168.1.42"


def test_an_interface_with_no_address_resolves_to_nothing():
    """The modem can be between leases. There is then nothing to bind to."""
    assert udpload.address_of("1: lo    inet6 ::1/128 scope host") is None
    assert udpload.address_of("") is None


def test_the_load_test_is_pinned_to_the_measured_link(monkeypatch):
    """The rig broadcasts its own Wi-Fi and measures a different interface.
    Without -B the test follows the routing table, which is right today and
    stops being right the moment the Pi gains a second route."""
    monkeypatch.setattr(udpload, "interface_address", lambda name: "10.0.0.7")
    worker = udpload.UdpLoadWorker(server="example.test", interface="eth0")
    cmd = worker.build_command()
    assert "-B" in cmd and cmd[cmd.index("-B") + 1] == "10.0.0.7"
    # Never the interface name: iperf3 rejects that outright and the test
    # would never start.
    assert "eth0" not in cmd


def test_an_unresolvable_interface_falls_back_to_the_routing_table(monkeypatch):
    """Worse than binding, but far better than a test that refuses to run."""
    monkeypatch.setattr(udpload, "interface_address", lambda name: None)
    worker = udpload.UdpLoadWorker(server="example.test", interface="eth0")
    assert "-B" not in worker.build_command()


# ---- saying why a test produced nothing ------------------------------------

def test_an_authorization_failure_names_both_of_its_causes():
    """iperf3's own message names neither, and one of them — a clock this Pi
    cannot keep on its own — is not the one anybody checks first. Measured
    against iperf3 3.16: a client 10s out authenticates, 11s out is rejected,
    with exactly the message a wrong password gets."""
    reason = udpload.parse_error("iperf3: error - test authorization failed")
    assert "username/password" in reason
    assert "RTC" in reason


def test_other_failures_are_passed_through_as_iperf3_worded_them():
    assert udpload.parse_error(
        "iperf3: error - unable to connect to server: Connection refused"
    ) == "unable to connect to server: Connection refused"


def test_ordinary_output_is_not_mistaken_for_a_failure():
    assert udpload.parse_error(RECEIVER_LINE) is None
    assert udpload.parse_error("") is None


# ---- the padding change in iperf3 3.17 -------------------------------------

def test_a_rejection_makes_the_worker_try_the_older_padding(monkeypatch):
    """3.17 changed the credential encryption from PKCS#1 to OAEP, and the two
    do not interoperate. The server reports the mismatch as an authorization
    failure — identical to a wrong password — and says "padding check failed"
    only in its own log. Nobody should have to read a log on another machine,
    so the rejection itself is what settles it."""
    monkeypatch.setattr(udpload, "iperf_supports_pkcs1", lambda: True)
    worker = UdpLoadWorker(server="example.test", username="rig",
                           public_key_path="/k")
    assert udpload.PKCS1_FLAG not in worker.build_command()

    worker._fail_with_reason("iperf3: error - test authorization failed", "quiet")
    assert udpload.PKCS1_FLAG in worker.build_command()


def test_an_older_iperf3_is_never_given_a_flag_it_lacks(monkeypatch):
    """Before 3.17 there is no flag at all — passing it would break a setup
    that was working."""
    monkeypatch.setattr(udpload, "iperf_supports_pkcs1", lambda: False)
    worker = UdpLoadWorker(server="example.test", username="rig",
                           public_key_path="/k")
    worker._fail_with_reason("iperf3: error - test authorization failed", "quiet")
    assert udpload.PKCS1_FLAG not in worker.build_command()


def test_the_fallback_is_tried_once_and_then_reported(monkeypatch):
    """If the older padding is refused too, it is a real credential problem
    and must be said out loud rather than retried forever."""
    monkeypatch.setattr(udpload, "iperf_supports_pkcs1", lambda: True)
    said: list[tuple[str, str]] = []
    worker = UdpLoadWorker(server="example.test", username="rig",
                           public_key_path="/k",
                           on_event=lambda level, msg: said.append((level, msg)))
    worker._fail_with_reason("iperf3: error - test authorization failed", "quiet")
    worker._fail_with_reason("iperf3: error - test authorization failed", "quiet")
    assert any(level == "error" for level, _ in said)


def test_the_padding_can_be_pinned_when_negotiation_is_not_wanted():
    forced = UdpLoadWorker(server="s", username="u", public_key_path="/k",
                           auth_padding="pkcs1")
    assert udpload.PKCS1_FLAG in forced.build_command()
    modern = UdpLoadWorker(server="s", username="u", public_key_path="/k",
                           auth_padding="oaep")
    assert udpload.PKCS1_FLAG not in modern.build_command()


# ---- the duty cycle --------------------------------------------------------

def test_the_uplink_claims_the_link_for_the_burst_plus_the_settling_seconds():
    """The modem holds roughly 1.75 Mbit of buffer, measured from a real walk's
    own median round trip, so ping keeps queueing behind the test for a second
    or two after the last datagram is handed over. A sample taken then is still
    describing the test."""
    worker = UdpLoadWorker(server="example.invalid", direction=UPLINK,
                           block_s=10, idle_s=20, settle_s=3)
    assert worker.loading is False
    worker._load_until = time.time() + 2
    assert worker.loading is True
    worker._load_until = time.time() - 0.01
    assert worker.loading is False


def test_downlink_never_claims_the_uplink():
    """-R has the *server* send, so the rig's uplink stays empty. Marking those
    seconds would throw away two thirds of a walk for nothing."""
    worker = UdpLoadWorker(server="example.invalid", direction=DOWNLINK)
    worker._load_until = time.time() + 30
    assert worker.loading is False


def test_the_offered_rate_is_carried_for_the_report():
    """The report needs it to tell a spot that could not carry the load apart
    from one that carried it cleanly, and the config string is the only place
    the rate is written down."""
    worker = UdpLoadWorker(server="example.invalid", direction=UPLINK, bitrate="750k")
    assert worker.offered_mbps == pytest.approx(0.75)
    assert worker.snapshot()["offered_mbps"] == pytest.approx(0.75)


def test_the_block_length_is_still_floored():
    """A burst too short to establish reports nothing at all."""
    worker = UdpLoadWorker(server="example.invalid", direction=UPLINK, block_s=1)
    assert worker.block_s == 5.0


def test_a_negative_gap_is_not_a_gap():
    worker = UdpLoadWorker(server="example.invalid", direction=UPLINK,
                           idle_s=-5, settle_s=-1)
    assert worker.idle_s == 0.0
    assert worker.settle_s == 0.0


# ---- the hang that was invisible -------------------------------------------

def test_a_test_that_stops_talking_is_killed_rather_than_hanging_the_worker(monkeypatch):
    """Reading a pipe has no timeout, so a hung iperf3 held run_once open for
    the rest of the walk. The worker then never returned, never restarted and
    never said anything, which looks exactly like working. On 2026-09-18 that
    cost a nine-minute walk: udp_up logged nothing and recorded 13 seconds of
    load out of 566."""
    monkeypatch.setattr(udpload, "STALL_AFTER_S", 1.0)
    events = []
    worker = UdpLoadWorker(server="example.invalid", direction=UPLINK,
                           on_event=lambda level, msg: events.append((level, msg)))
    monkeypatch.setattr(worker, "build_command", lambda: ["sleep", "30"])
    monkeypatch.setattr(worker, "build_env", lambda: None)

    started = time.time()
    proc = worker._launch()
    proc.wait(timeout=15)                  # the watchdog must end this, not sleep
    elapsed = time.time() - started

    assert elapsed < 10, f"the hung process ran for {elapsed:.1f}s"
    assert worker._stalls == 1
    assert any("stopped responding" in message for _, message in events)


def test_a_test_that_keeps_talking_is_left_alone(monkeypatch):
    """The watchdog must not shoot a healthy long-running test. Downlink runs
    with -t 0 and is meant to stream for the whole walk."""
    monkeypatch.setattr(udpload, "STALL_AFTER_S", 2.0)
    worker = UdpLoadWorker(server="example.invalid", direction=DOWNLINK)
    monkeypatch.setattr(
        worker, "build_command",
        lambda: ["sh", "-c", "for i in 1 2 3 4 5 6; do echo tick; sleep 0.5; done"])
    monkeypatch.setattr(worker, "build_env", lambda: None)

    proc = worker._launch()
    for line in proc.stdout:
        worker._consume(line)
    proc.wait(timeout=5)

    assert proc.returncode == 0, "a talking process was killed"
    assert worker._stalls == 0


def test_the_stall_count_is_visible(monkeypatch):
    """A worker that keeps being shot is a different problem from one that
    never starts, and the dashboard should be able to tell them apart."""
    worker = UdpLoadWorker(server="example.invalid", direction=UPLINK)
    assert worker.snapshot()["stalls"] == 0
