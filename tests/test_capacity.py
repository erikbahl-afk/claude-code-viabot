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
