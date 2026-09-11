from viabot_survey.router_client import (LuciRouterClient, NullRouterClient,
                                         UbusRouterClient, _parse_at_output,
                                         build_client, normalize_signal)


def test_null_client_collects_nothing():
    assert NullRouterClient().fetch() == {}


def test_normalize_finds_fields_whatever_the_nesting():
    payload = {"modem": {"signal": {"rsrp": -95, "rsrq": "-11 dB", "sinr": 12.5},
                         "cellid": "0x1A2B3C", "network_type": "5G-NSA"}}
    fields = normalize_signal(payload)
    assert fields["rsrp"] == -95.0
    assert fields["rsrq"] == -11.0          # units stripped
    assert fields["sinr"] == 12.5
    assert fields["cell_id"] == "0x1A2B3C"
    assert fields["tech"] == "5G-NSA"


def test_shallower_matches_win():
    """A top-level reading must not be replaced by one buried in a per-band
    detail list — firmwares often report both."""
    payload = {"rsrp": -80, "bands": [{"rsrp": -120}, {"rsrp": -119}]}
    assert normalize_signal(payload)["rsrp"] == -80.0


def test_unrecognised_payload_yields_nothing():
    assert normalize_signal({"uptime": 1234, "hostname": "HL"}) == {}


def test_booleans_are_not_mistaken_for_readings():
    assert "rssi" not in normalize_signal({"rssi": True})


def test_csq_is_converted_to_dbm():
    fields = normalize_signal(_parse_at_output("+CSQ: 18,99\n\nOK\n"))
    assert fields["rssi"] == -77.0          # -113 + 2*18


def test_csq_99_means_unknown_and_is_dropped():
    assert "rssi" not in normalize_signal(_parse_at_output("+CSQ: 99,99\nOK\n"))


def test_build_client_selects_the_configured_backend():
    assert isinstance(build_client({"client": "null"}, "192.168.1.1"), NullRouterClient)
    assert isinstance(build_client({"client": "ubus"}, "192.168.1.1"), UbusRouterClient)
    assert isinstance(build_client({"client": "luci"}, "192.168.1.1"), LuciRouterClient)


def test_build_client_rejects_a_typo():
    try:
        build_client({"client": "ubuss"}, "192.168.1.1")
    except ValueError as exc:
        assert "ubuss" in str(exc)
    else:
        raise AssertionError("expected a ValueError")
