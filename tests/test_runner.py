import time

from viabot_survey.runner import classify, slugify


THRESHOLDS = {"degraded_loss_pct": 5, "degraded_rtt_ms": 200,
              "bad_loss_pct": 20, "bad_rtt_ms": 500, "dead_after_s": 5}


def test_clean_link_is_good():
    assert classify(0.0, 48.0, THRESHOLDS) == "good"


def test_latency_alone_degrades():
    assert classify(0.0, 250.0, THRESHOLDS) == "degraded"
    assert classify(0.0, 600.0, THRESHOLDS) == "bad"


def test_loss_alone_degrades():
    assert classify(10.0, 50.0, THRESHOLDS) == "degraded"
    assert classify(30.0, 50.0, THRESHOLDS) == "bad"


def test_total_loss_becomes_dead_only_after_the_configured_streak():
    """A one-second blip while walking past a pillar is not a dead zone."""
    assert classify(100.0, None, THRESHOLDS, dead_streak_s=1.0) == "bad"
    assert classify(100.0, None, THRESHOLDS, dead_streak_s=4.0) == "bad"
    assert classify(100.0, None, THRESHOLDS, dead_streak_s=5.0) == "dead"


def test_no_data_is_unknown_not_good():
    assert classify(None, None, THRESHOLDS) == "unknown"


def test_slugify_makes_safe_run_ids():
    assert slugify("Sunset Garage L2") == "sunset-garage-l2"
    assert slugify("  Ramp / Level 3!! ") == "ramp-level-3"
    assert slugify("") == ""
    assert len(slugify("x" * 100)) == 32


def test_sample_collection_records_signal_and_video_pointers(runner, storage, monkeypatch):
    monkeypatch.setattr("viabot_survey.runner.sysinfo.clock_synced", lambda: True)
    runner.ping.enabled = True
    monkeypatch.setattr(runner.ping, "snapshot",
                        lambda now=None: {"rtt_ms": 55.0, "loss_pct": 0.0, "jitter_ms": 2.0})
    runner.router.enabled = True
    monkeypatch.setattr(runner.router, "sample_fields",
                        lambda: {"rsrp": -92.0, "rsrq": None, "sinr": 9.0, "rssi": None,
                                 "band": "n41", "cell_id": "ABC", "tech": "5G"})
    monkeypatch.setattr(runner.camera, "locate", lambda ts: ("20260911-140000.mkv", 12.5))

    run = runner.start_run(label="test")
    runner.collect_sample()
    rows = list(storage.iter_samples(run["id"]))
    runner.stop_run()

    assert len(rows) == 1
    assert rows[0]["status"] == "good"
    assert rows[0]["rsrp"] == -92.0
    assert rows[0]["band"] == "n41"
    assert rows[0]["video_file"] == "20260911-140000.mkv"
    assert rows[0]["video_offset_s"] == 12.5
    assert rows[0]["clock_synced"] == 1


def test_dead_streak_accumulates_then_resets(runner, monkeypatch):
    runner.ping.enabled = True
    state = {"loss": 100.0}
    monkeypatch.setattr(runner.ping, "snapshot",
                        lambda now=None: {"rtt_ms": None, "loss_pct": state["loss"],
                                          "jitter_ms": None})
    for _ in range(6):
        sample = runner.collect_sample()
    assert sample["status"] == "dead"
    state["loss"] = 0.0
    assert runner.collect_sample()["dead_streak_s"] == 0.0


def test_second_run_is_refused_while_one_is_active(runner):
    runner.start_run(label="First")
    try:
        runner.start_run(label="Second")
    except RuntimeError as exc:
        assert "already in progress" in str(exc)
    finally:
        runner.stop_run()


def test_pausing_stops_samples_being_recorded(runner, storage, monkeypatch):
    """Paused time must leave no trace at all — the headline result is a
    percentage of time walked, so standing still must not count as coverage."""
    runner.ping.enabled = True
    monkeypatch.setattr(runner.ping, "snapshot",
                        lambda now=None: {"rtt_ms": 50.0, "loss_pct": 0.0, "jitter_ms": 1.0})
    run = runner.start_run(label="Pause test")
    runner.collect_sample()
    runner.collect_sample()
    assert runner.pause() is True
    runner.collect_sample()
    runner.collect_sample()
    runner.collect_sample()
    assert runner.resume() is True
    runner.collect_sample()
    rows = list(storage.iter_samples(run["id"]))
    runner.stop_run()
    assert len(rows) == 3          # the three paused seconds are simply absent


def test_pause_is_idempotent_and_needs_a_run(runner):
    assert runner.pause() is False
    assert runner.resume() is False
    runner.start_run(label="L2")
    assert runner.pause() is True
    assert runner.pause() is False
    assert runner.resume() is True
    assert runner.resume() is False
    runner.stop_run()


def test_startup_closes_a_run_left_open_by_a_power_cut(config, storage):
    """A run still open in the database means the Pi lost power mid-walk.
    Reopening it would leave a gap in the timeline that reads like a dead zone."""
    from viabot_survey.runner import SurveyRunner

    storage.create_run("orphan", started_at=time.time() - 600)
    survey = SurveyRunner(config, storage)
    try:
        survey.start()
        assert storage.get_run("orphan")["ended_at"] is not None
        assert storage.active_run() is None
    finally:
        survey.shutdown()


def test_an_interrupted_run_is_analysed_not_discarded(config, storage, monkeypatch):
    """Power dies mid-walk. Everything recorded up to that point is good data,
    and throwing it away means driving back to the garage."""
    import threading

    from viabot_survey.runner import SurveyRunner

    base = time.time() - 300
    storage.create_run("cut-off", started_at=base)
    for offset in range(100):
        dead = 40 <= offset < 70
        storage.add_sample("cut-off", base + offset,
                           rtt_ms=None if dead else 45.0,
                           loss_pct=100.0 if dead else 0.0,
                           status="dead" if dead else "good")

    survey = SurveyRunner(config, storage)
    done = threading.Event()
    original = survey.analyse_run

    def watched(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            done.set()

    monkeypatch.setattr(survey, "analyse_run", watched)
    try:
        survey.start()
        assert done.wait(timeout=10), "the interrupted run was never analysed"
    finally:
        survey.shutdown()

    run = storage.get_run("cut-off")
    assert run["ended_at"] is not None
    assert run["runnable_pct"] == 70.0          # 30 of 100 seconds unusable
    zones = storage.list_dead_zones("cut-off")
    assert len(zones) == 1
    assert zones[0]["duration_s"] == 30.0
    assert any("cut off mid-walk" in e["message"] for e in storage.recent_events())


def test_recovery_is_silent_when_nothing_was_interrupted(config, storage):
    from viabot_survey.runner import SurveyRunner

    survey = SurveyRunner(config, storage)
    try:
        survey.start()
        assert storage.active_run() is None
    finally:
        survey.shutdown()
    assert not any("cut off" in e["message"] for e in storage.recent_events())
