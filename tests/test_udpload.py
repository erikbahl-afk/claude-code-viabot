"""The UDP load test.

The interval lines in here are verbatim iperf3 output, captured from a real
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

from viabot_survey.workers.udpload import UdpLoadWorker, parse_interval

# Real iperf3 3.16 output, reverse UDP, 1200-byte datagrams.
REAL = [
    "[  5]   0.00-1.00   sec   123 KBytes  1.01 Mbits/sec  0.062 ms  0/105 (0%)  ",
    "[  5]   1.00-2.00   sec   122 KBytes   998 Kbits/sec  0.027 ms  3/104 (2.9%)  ",
    "[  5]   2.00-3.00   sec   122 KBytes   998 Kbits/sec  0.030 ms  0/104 (0%)  ",
]
SUMMARY = "[  5]   0.00-6.00   sec   732 KBytes  1.00 Mbits/sec  0.030 ms  0/625 (0%)  receiver"
HEADER = "[ ID] Interval           Transfer     Bitrate         Jitter    Lost/Total Datagrams"


# ---- parsing ---------------------------------------------------------------

def test_a_real_interval_line_parses():
    reading = parse_interval(REAL[0])
    assert reading["jitter_ms"] == 0.062
    assert reading["loss_pct"] == 0.0
    assert reading["total"] == 105
    assert round(reading["mbps"], 2) == 1.01


def test_kilobit_rates_are_scaled_like_megabit_ones():
    """iperf3 switches units on its own once a link degrades, and a slow garage
    is exactly where it will start printing Kbits/sec."""
    reading = parse_interval(REAL[1])
    assert round(reading["mbps"], 3) == 0.998
    assert reading["loss_pct"] == 2.9


def test_the_summary_lines_are_not_mistaken_for_readings():
    """They have the identical shape and cover the whole run, so counting one
    would put a run-length average into a one-second sample."""
    assert parse_interval(SUMMARY) is None
    assert parse_interval(HEADER) is None
    assert parse_interval("iperf Done.") is None
    assert parse_interval("") is None


# ---- the command -----------------------------------------------------------

def test_the_command_asks_for_what_teleop_actually_looks_like():
    argv = UdpLoadWorker("10.0.0.1").build_command()
    assert "-u" in argv                                   # UDP, not TCP
    assert argv[argv.index("-l") + 1] == "1200"           # RTP-sized datagrams
    assert argv[argv.index("-t") + 1] == "0"              # for the whole walk
    assert "-R" in argv                                   # server sends, rig receives
    assert "--forceflush" in argv                         # readings arrive live


def test_the_default_datagram_size_is_not_iperf3s():
    """iperf3 defaults to 32 KB datagrams, which IP fragments into two dozen
    packets. Losing any one marks the whole datagram lost, so loss reads several
    times worse than a real video packet would see, and every garage looks
    terrible."""
    assert UdpLoadWorker("10.0.0.1").datagram_bytes == 1200


def test_uploading_drops_the_reverse_flag():
    argv = UdpLoadWorker("10.0.0.1", direction="upload").build_command()
    assert "-R" not in argv


# ---- the data ceiling ------------------------------------------------------

def test_the_run_budget_is_a_hard_stop():
    """This is the only thing standing between an unknown SIM plan and a
    several-hundred-megabyte walk."""
    worker = UdpLoadWorker("10.0.0.1", run_data_budget_mb=1)
    assert not worker.budget_spent
    for _ in range(10):
        worker._consume("[  5] 0.00-1.00 sec 200 KBytes 1.64 Mbits/sec 0.0 ms 0/170 (0%)")
    assert worker.budget_spent


def test_each_walk_gets_a_fresh_allowance():
    worker = UdpLoadWorker("10.0.0.1", run_data_budget_mb=1)
    for _ in range(10):
        worker._consume("[  5] 0.00-1.00 sec 200 KBytes 1.64 Mbits/sec 0.0 ms 0/170 (0%)")
    assert worker.budget_spent
    worker.begin_run()
    assert not worker.budget_spent


def test_no_budget_means_no_ceiling():
    assert not UdpLoadWorker("10.0.0.1", run_data_budget_mb=None).budget_spent


# ---- what reaches a sample -------------------------------------------------

def test_a_stale_reading_is_dropped_rather_than_repeated():
    """When the link dies badly the iperf3 control channel — which is TCP —
    dies with it and the stream stops. Carrying the last good number forward
    would paint the worst spot in the garage as the healthiest."""
    worker = UdpLoadWorker("10.0.0.1")
    worker._consume(REAL[0])
    assert worker.sample_fields()["udp_loss_pct"] == 0.0

    worker._latest_ts -= 30
    assert worker.sample_fields() == {"udp_jitter_ms": None, "udp_loss_pct": None,
                                      "udp_mbps": None}


def test_a_worker_with_no_server_configured_stays_off():
    assert not UdpLoadWorker(None).enabled


# ---- against a real iperf3, when there is one ------------------------------

requires_iperf3 = pytest.mark.skipif(
    shutil.which("iperf3") is None, reason="iperf3 is not installed")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@requires_iperf3
def test_readings_flow_from_a_real_iperf3_server():
    """End to end against the actual binary. The flags are the part most likely
    to be subtly wrong — a missing --forceflush produces nothing for the whole
    walk and then everything at once, which is invisible to a parser test."""
    port = _free_port()
    server = subprocess.Popen(["iperf3", "-s", "-p", str(port), "-1"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    worker = UdpLoadWorker("127.0.0.1", port=port, bitrate="1M",
                           run_data_budget_mb=None)
    try:
        time.sleep(0.5)
        worker.start()
        deadline = time.time() + 25
        while time.time() < deadline:
            fields = worker.sample_fields()
            if fields["udp_loss_pct"] is not None:
                break
            time.sleep(0.25)
        else:
            raise AssertionError(f"no reading arrived; last line: {worker._last_line!r}")

        assert fields["udp_jitter_ms"] >= 0
        assert 0 <= fields["udp_loss_pct"] <= 100
        assert fields["udp_mbps"] > 0
        assert worker.snapshot()["streaming"] is True
    finally:
        worker.stop()
        server.terminate()
        server.wait(timeout=5)


# ---- authentication --------------------------------------------------------

def test_the_password_never_reaches_the_command_line():
    """A survey walk runs this for half an hour; argv is readable from ps for
    every second of it."""
    worker = UdpLoadWorker("10.0.0.1", username="rig", password="hunter2",
                           public_key_path="/etc/viabot/iperf.pub")
    assert not any("hunter2" in arg for arg in worker.build_command())
    assert worker.build_env()["IPERF3_PASSWORD"] == "hunter2"


def test_credentials_are_only_sent_when_both_halves_are_configured():
    """Half-configured auth against an authenticating server fails in a way
    that reads as a network fault, so do not half-send it."""
    argv = UdpLoadWorker("10.0.0.1", username="rig").build_command()
    assert "--username" not in argv
    assert UdpLoadWorker("10.0.0.1", username="rig").build_env() is None


@requires_iperf3
def test_a_real_authenticated_server_accepts_the_rig_and_nobody_else(tmp_path):
    """iperf3's own RSA authentication, exercised for real — the alternative is
    an open server on the internet that anyone who finds the port can spend the
    bandwidth allowance on."""
    import hashlib

    private = tmp_path / "private.pem"
    public = tmp_path / "public.pem"
    credentials = tmp_path / "credentials.csv"
    for argv in (
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
         "rsa_keygen_bits:2048", "-out", str(private), "-outform", "PEM"],
        ["openssl", "rsa", "-in", str(private), "-outform", "PEM", "-pubout",
         "-out", str(public)],
    ):
        if subprocess.run(argv, capture_output=True).returncode != 0:
            pytest.skip("openssl is not available")

    user, password = "viabot-rig", "a-long-random-password"
    digest = hashlib.sha256(f"{{{user}}}{password}".encode()).hexdigest()
    credentials.write_text(f"{user},{digest}\n")

    port = _free_port()
    server = subprocess.Popen(
        ["iperf3", "-s", "-p", str(port),
         "--rsa-private-key-path", str(private),
         "--authorized-users-path", str(credentials)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    worker = UdpLoadWorker("127.0.0.1", port=port, bitrate="1M",
                           run_data_budget_mb=None, username=user,
                           password=password, public_key_path=str(public))
    try:
        time.sleep(0.5)
        worker.start()
        deadline = time.time() + 25
        while time.time() < deadline:
            if worker.sample_fields()["udp_loss_pct"] is not None:
                break
            time.sleep(0.25)
        else:
            raise AssertionError(f"authenticated run produced nothing; "
                                 f"last line: {worker._last_line!r}")
        worker.stop()

        # And a client without credentials is turned away.
        stranger = subprocess.run(
            ["iperf3", "-c", "127.0.0.1", "-p", str(port), "-u", "-t", "1"],
            capture_output=True, text=True, timeout=20)
        assert "authorization failed" in (stranger.stdout + stranger.stderr)
    finally:
        worker.stop()
        server.terminate()
        server.wait(timeout=5)
