import time

from viabot_survey.workers.ping import PingWorker


def make_worker(**kwargs):
    kwargs.setdefault("target", "8.8.8.8")
    kwargs.setdefault("window_s", 10)
    return PingWorker(**kwargs)


def test_command_pins_the_uplink_interface():
    cmd = make_worker(interface="eth0").build_command()
    assert cmd[0] == "ping"
    # -O is what gives us per-packet loss in real time rather than at the end.
    assert "-O" in cmd and "-D" in cmd and "-n" in cmd
    assert cmd[cmd.index("-I") + 1] == "eth0"
    assert cmd[-1] == "8.8.8.8"


def test_command_omits_interface_when_unset():
    assert "-I" not in make_worker(interface=None).build_command()


def test_parses_replies_and_computes_stats():
    worker = make_worker()
    now = time.time()
    for i in range(5):
        worker.handle_line(
            f"[{now + i:.6f}] 64 bytes from 8.8.8.8: icmp_seq={i} ttl=118 time={40 + i * 2}.0 ms")
    snapshot = worker.snapshot(now=now + 5)
    assert snapshot["sent"] == 5
    assert snapshot["received"] == 5
    assert snapshot["loss_pct"] == 0.0
    assert snapshot["rtt_ms"] == 48.0
    assert snapshot["avg_rtt_ms"] == 44.0
    assert snapshot["jitter_ms"] == 2.0


def test_counts_unanswered_packets_as_loss():
    worker = make_worker()
    now = time.time()
    worker.handle_line(f"[{now:.6f}] 64 bytes from 8.8.8.8: icmp_seq=1 ttl=118 time=40.0 ms")
    worker.handle_line(f"[{now + 1:.6f}] no answer yet for icmp_seq=2")
    worker.handle_line(f"[{now + 2:.6f}] no answer yet for icmp_seq=3")
    worker.handle_line(f"[{now + 3:.6f}] 64 bytes from 8.8.8.8: icmp_seq=4 ttl=118 time=44.0 ms")
    snapshot = worker.snapshot(now=now + 3)
    assert snapshot["sent"] == 4
    assert snapshot["received"] == 2
    assert snapshot["loss_pct"] == 50.0


def test_late_reply_supersedes_the_no_answer_line():
    """ping prints 'no answer yet' at timeout and then the reply if it arrives.
    Counting both would invent packet loss that never happened."""
    worker = make_worker()
    now = time.time()
    worker.handle_line(f"[{now:.6f}] no answer yet for icmp_seq=7")
    worker.handle_line(f"[{now + 1:.6f}] 64 bytes from 8.8.8.8: icmp_seq=7 ttl=118 time=900.0 ms")
    snapshot = worker.snapshot(now=now + 1)
    assert snapshot["sent"] == 1
    assert snapshot["received"] == 1
    assert snapshot["loss_pct"] == 0.0


def test_unreachable_after_a_reply_is_not_double_counted():
    worker = make_worker()
    now = time.time()
    worker.handle_line(f"[{now:.6f}] 64 bytes from 8.8.8.8: icmp_seq=3 ttl=118 time=40.0 ms")
    worker.handle_line(f"[{now + 1:.6f}] From 192.168.1.1 icmp_seq=3 Destination Host Unreachable")
    snapshot = worker.snapshot(now=now + 1)
    assert snapshot["received"] == 1
    assert snapshot["loss_pct"] == 0.0


def test_total_silence_reports_full_loss_not_a_stale_rtt():
    """Walking into a dead zone means ping simply stops printing. The dashboard
    must not keep showing the last good RTT."""
    worker = make_worker(interval_s=1.0)
    now = time.time()
    worker.handle_line(f"[{now:.6f}] 64 bytes from 8.8.8.8: icmp_seq=1 ttl=118 time=40.0 ms")
    snapshot = worker.snapshot(now=now + 30)
    assert snapshot["rtt_ms"] is None
    assert snapshot["loss_pct"] == 100.0
    assert snapshot["silent_for_s"] == 30.0


def test_empty_window_reports_unknown_rather_than_zero():
    assert make_worker().snapshot(now=time.time())["loss_pct"] is None


def test_fatal_resolver_error_marks_the_worker_failed():
    worker = make_worker(target="nope.invalid")
    worker.handle_line("ping: nope.invalid: Name or service not known")
    assert worker.state == "failed"
    assert "Name or service not known" in worker.error


def test_old_results_are_pruned():
    worker = make_worker(window_s=5)
    now = time.time()
    for i in range(300):
        worker.handle_line(
            f"[{now + i:.6f}] 64 bytes from 8.8.8.8: icmp_seq={i} ttl=118 time=40.0 ms")
    # Retention is a multiple of the window, so memory cannot grow without bound.
    assert len(worker._results) <= 70
