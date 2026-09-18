"""The one-off capacity probe's arithmetic and verdicts.

The probe itself needs a cellular modem and a server in Dallas. The part that
decides what the numbers *mean* does not, so it lives here and is tested
against the shapes iperf3 3.16 really emits.
"""

from __future__ import annotations

import json

from viabot_survey import capacity

# Trimmed from real `iperf3 -c ... -J` output, keys and nesting untouched.
TCP_DOC = {
    "end": {
        "sum_sent": {"bits_per_second": 2_410_000.0, "retransmits": 12,
                     "seconds": 10.0},
        "sum_received": {"bits_per_second": 2_180_000.0, "seconds": 10.0},
    }
}

# A UDP sender's summary. Loss and jitter are here because the server reported
# them back — the sending end cannot measure its own.
UDP_DOC = {
    "end": {"sum": {"bits_per_second": 1_007_600.99,
                    "jitter_ms": 0.0434, "lost_percent": 0}}
}

REFUSED = {"error": "unable to connect to server - Connection refused"}


def write(tmp_path, name, doc):
    (tmp_path / name).write_text(json.dumps(doc))


# ---- rate spelling ---------------------------------------------------------

def test_rates_are_read_in_iperf3s_own_spelling():
    assert capacity.parse_rate_mbps("5M") == 5.0
    assert capacity.parse_rate_mbps("1.5M") == 1.5
    assert capacity.parse_rate_mbps("300k") == 0.3
    assert capacity.parse_rate_mbps("anything else") is None


# ---- reading a result ------------------------------------------------------

def test_throughput_is_what_arrived_not_what_was_sent():
    """sum_sent counts bytes put on the wire, retransmissions included. On a
    cellular link those differ by enough to matter."""
    assert capacity.tcp_result(TCP_DOC)["mbps"] == 2.18
    assert capacity.tcp_result(TCP_DOC)["retransmits"] == 12


def test_a_refused_connection_reads_as_a_reason_not_a_zero():
    """A server that is down must never render as 'this link carries 0 Mbit/s',
    which is a measurement, and a wrong one."""
    assert "Connection refused" in capacity.tcp_result(REFUSED)["error"]
    assert "mbps" not in capacity.tcp_result(REFUSED)
    assert capacity.tcp_result(None)["error"] == "no result"


def test_loss_and_jitter_come_back_from_the_receiving_end():
    result = capacity.udp_result(UDP_DOC)
    assert round(result["mbps"], 2) == 1.01
    assert result["loss_pct"] == 0
    assert round(result["jitter_ms"], 3) == 0.043


# ---- what the numbers mean -------------------------------------------------

def test_a_ceiling_below_the_wanted_rate_is_stated_plainly():
    """The whole point of running this. If the link cannot carry the stream,
    the output has to say so in words, not leave it as a ratio to work out."""
    assert "BELOW 5 Mbit/s" in capacity.headroom(3.2, 5.0)
    assert "cannot carry" in capacity.headroom(3.2, 5.0)


def test_barely_enough_is_not_reported_as_fine():
    assert "nothing spare" in capacity.headroom(5.4, 5.0)
    assert "headroom" in capacity.headroom(12.0, 5.0)


def test_headroom_says_nothing_when_there_is_nothing_to_compare():
    assert capacity.headroom(None, 5.0) == ""
    assert capacity.headroom(8.0, None) == ""


# ---- the whole rendering ---------------------------------------------------

def test_the_report_names_both_directions_and_the_caveat(tmp_path):
    write(tmp_path, "up.json", TCP_DOC)
    write(tmp_path, "down.json", TCP_DOC)
    out = capacity.render(tmp_path, configured_up="1.5M",
                          configured_down="300k", udp_rate=None)
    assert "uplink" in out and "downlink" in out
    # One spot is not a garage, and the output must not let anyone forget it.
    assert "one spot, not a garage" in out


def test_a_missing_udp_run_is_simply_absent(tmp_path):
    write(tmp_path, "up.json", TCP_DOC)
    write(tmp_path, "down.json", TCP_DOC)
    out = capacity.render(tmp_path, configured_up="1.5M",
                          configured_down="300k", udp_rate=None)
    assert "Constant" not in out


def test_the_udp_verdict_follows_the_loss(tmp_path):
    write(tmp_path, "up.json", TCP_DOC)
    write(tmp_path, "down.json", TCP_DOC)
    write(tmp_path, "uup.json", UDP_DOC)
    write(tmp_path, "udown.json", UDP_DOC)
    out = capacity.render(tmp_path, configured_up="1.5M",
                          configured_down="300k", udp_rate="5M")
    assert "carries it on the uplink" in out

    lossy = {"end": {"sum": {"bits_per_second": 2_000_000.0,
                             "jitter_ms": 40.0, "lost_percent": 61.0}}}
    write(tmp_path, "uup.json", lossy)
    out = capacity.render(tmp_path, configured_up="1.5M",
                          configured_down="300k", udp_rate="5M")
    assert "cannot carry it" in out


# ---- the failure that actually happened -------------------------------------

AUTH_FAILED = {"error": "test authorization failed"}


def test_a_skewed_clock_is_named_as_the_cause_rather_than_listed():
    """iperf3 signs each test with a timestamp and rejects a client more than
    10s out — with the same message a wrong password gets. The Pi has no RTC,
    so this is the likely cause and the last one anyone checks."""
    advice = "\n".join(capacity.auth_advice(-847.0))
    assert "-847s" in advice
    assert "timesyncd" in advice
    # Not a list of possibilities when we already know which one it is.
    assert "Three things" not in advice


def test_a_good_clock_rules_itself_out():
    advice = "\n".join(capacity.auth_advice(2.0))
    assert "Three things" in advice
    assert "so it is not that" in advice


def test_an_unknown_clock_leaves_the_causes_open():
    advice = "\n".join(capacity.auth_advice(None))
    assert "Three things" in advice
    assert "could not be checked" in advice


def test_the_version_mismatch_that_actually_happened_is_named():
    """Every credential correct, clock exact, and still rejected: iperf3 3.17
    changed the padding and the server reports the mismatch as an
    authorization failure. Only the server's log says otherwise."""
    advice = "\n".join(capacity.auth_advice(0.0))
    assert "3.17" in advice
    assert "iperf3 --version" in advice


def test_an_authorization_failure_explains_itself_in_the_report(tmp_path):
    """The bare iperf3 message names neither of its two causes."""
    write(tmp_path, "up.json", AUTH_FAILED)
    write(tmp_path, "down.json", AUTH_FAILED)
    out = capacity.render(tmp_path, configured_up="1.5M", configured_down="300k",
                          udp_rate=None, skew_s=-847.0)
    assert "test authorization failed" in out     # what iperf3 said
    assert "timesyncd" in out                     # and what to do about it


def test_a_working_run_carries_no_auth_advice(tmp_path):
    write(tmp_path, "up.json", TCP_DOC)
    write(tmp_path, "down.json", TCP_DOC)
    out = capacity.render(tmp_path, configured_up="1.5M", configured_down="300k",
                          udp_rate=None, skew_s=0.0)
    assert "rejected the credentials" not in out


# ---- the uplink figure that was fiction ------------------------------------

SERVER_LOG = """Server output:
[  5]   0.00-1.00   sec   160 KBytes  1.31 Mbits/sec  0.512 ms  1064/1200 (89%)
[  5]   1.00-2.00   sec   162 KBytes  1.33 Mbits/sec  0.488 ms  1050/1190 (88%)
"""


def test_the_uplink_figure_comes_from_the_server_not_the_sender():
    """Only the far end knows what arrived. Reading the client's own summary
    produced 12 Mbit/s "delivered" at 0.0% loss over a link the same run had
    measured at 1.64 Mbit/s — every uplink result was exactly the offered rate
    with exactly no loss, because a flooded uplink stops the end-of-test
    exchange getting back and iperf3 falls back to what it sent."""
    result = capacity.udp_result({
        "server_output_text": SERVER_LOG,
        "end": {"sum": {"bits_per_second": 12e6, "lost_percent": 0.0}},
    })
    assert round(result["mbps"], 2) == 1.32        # what arrived
    assert round(result["loss_pct"], 1) == 88.5    # not 0.0
    assert "unverified" not in result


def test_a_sender_only_figure_is_labelled_as_one():
    """When the server never reported back, the number is still shown — it is
    the only one there is — but never as though it were a measurement."""
    result = capacity.udp_result(
        {"end": {"sum": {"bits_per_second": 12e6, "lost_percent": 0.0}}})
    assert result["unverified"] is True
    rendered = capacity._line("uplink", result, udp=True)
    assert "what was SENT" in rendered
    # And it must not print a loss figure it did not measure.
    assert "loss" not in rendered
