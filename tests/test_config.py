import pytest
import yaml

from viabot_survey import config as config_module


def test_example_config_is_valid_yaml():
    data = yaml.safe_load(config_module.EXAMPLE_PATH.read_text())
    assert isinstance(data, dict)
    for section in ("ap", "uplink", "ping", "iperf3", "camera", "web", "update"):
        assert section in data, f"{section} missing from the example config"


def test_iperf_ships_disabled():
    """Throughput testing is the only thing that costs cellular data; it must
    stay off until an iperf3 server is deliberately configured."""
    data = yaml.safe_load(config_module.EXAMPLE_PATH.read_text())
    assert data["iperf3"]["enabled"] is False
    assert data["iperf3"]["server"] is None


def test_local_file_overlays_example(tmp_path):
    local = tmp_path / "config.yaml"
    local.write_text("ping:\n  target: 1.1.1.1\n")
    cfg = config_module.load(path=local, environ={})
    assert cfg["ping"]["target"] == "1.1.1.1"
    # Untouched keys still come from the example defaults.
    assert cfg["ping"]["interval_s"] == 1.0
    assert cfg["camera"]["mode"] == "overlay"


def test_env_override_coerces_types(tmp_path):
    cfg = config_module.load(path=tmp_path / "nope.yaml", environ={
        "VIABOT_PING_TARGET": "9.9.9.9",
        "VIABOT_PING_INTERVAL_S": "2.5",
        "VIABOT_CAMERA_ENABLED": "false",
        "VIABOT_CAMERA_WIDTH": "640",
        "VIABOT_IPERF3_SERVER": "iperf.example.com",
    })
    assert cfg["ping"]["target"] == "9.9.9.9"
    assert cfg["ping"]["interval_s"] == 2.5
    assert cfg["camera"]["enabled"] is False
    assert cfg["camera"]["width"] == 640
    assert cfg["iperf3"]["server"] == "iperf.example.com"


def test_unknown_env_var_warns_but_does_not_raise(tmp_path):
    cfg = config_module.load(path=tmp_path / "nope.yaml",
                             environ={"VIABOT_PING_NOSUCHKEY": "x"})
    assert any("NOSUCHKEY" in warning for warning in cfg.warnings)


def test_bad_boolean_is_reported_not_applied(tmp_path):
    cfg = config_module.load(path=tmp_path / "nope.yaml",
                             environ={"VIABOT_CAMERA_ENABLED": "perhaps"})
    assert cfg["camera"]["enabled"] is True
    assert any("CAMERA_ENABLED" in warning for warning in cfg.warnings)


def test_non_mapping_local_config_is_rejected(tmp_path):
    local = tmp_path / "config.yaml"
    local.write_text("- just\n- a list\n")
    with pytest.raises(ValueError):
        config_module.load(path=local, environ={})
