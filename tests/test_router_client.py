from viabot_survey.router_client import (LuciRouterClient, NullRouterClient,
                                         UbusRouterClient, _parse_at_output,
                                         parse_qeng,
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


# ---- Quectel +QENG serving-cell parsing ------------------------------------
#
# The fixture is a real line from the rig's own EP06-A, read over the router's
# /dev/ttyUSB2. Keeping the modem's exact output here is the point: the field
# order is the one thing in this parser that cannot be reasoned out from first
# principles, and a synthetic example would only prove the parser agrees with
# whatever we assumed when we wrote it.

REAL_QENG = (
    'AT+QENG="servingcell"\r\n'
    '+QENG: "servingcell","NOCONN","LTE","FDD",310,260,1452808,315,5035,'
    '12,2,2,3AFD,-104,-11,-78,12,20\r\n'
    '\r\nOK\r\n'
)


def test_a_real_serving_cell_line_parses():
    fields = parse_qeng(REAL_QENG)
    assert fields["tech"] == "LTE"
    assert fields["rsrp"] == -104.0
    assert fields["rsrq"] == -11.0
    assert fields["rssi"] == -78.0
    assert fields["sinr"] == 12.0
    assert fields["band"] == "12"
    assert fields["cell_id"] == "1452808"


def test_the_echo_and_ok_around_the_reading_are_ignored():
    """The probe reads the port raw, so the command echo and the trailing OK
    arrive with the answer."""
    assert parse_qeng(REAL_QENG) == parse_qeng(
        '+QENG: "servingcell","NOCONN","LTE","FDD",310,260,1452808,315,5035,'
        '12,2,2,3AFD,-104,-11,-78,12,20'
    )


def test_the_cell_id_stays_a_string():
    """Quectel does not say whether this field is decimal or hex, and the
    reading we have is ambiguous. It is only ever compared for equality, to
    spot handovers, so record it verbatim rather than guess at a base."""
    assert isinstance(parse_qeng(REAL_QENG)["cell_id"], str)


def test_bandwidth_is_translated_out_of_quectels_index():
    assert parse_qeng(REAL_QENG)["dl_bandwidth_mhz"] == 5.0


def test_a_searching_modem_reports_nothing_rather_than_a_bad_reading():
    """With no serving cell there is no signal to record. Writing zeros here
    would put a fake dead zone in the report."""
    assert parse_qeng('+QENG: "servingcell","SEARCH"') == {}


def test_an_error_reply_yields_nothing():
    assert parse_qeng("AT+QENG=\"servingcell\"\r\nERROR\r\n") == {}


def test_unrelated_output_yields_nothing():
    assert parse_qeng("OK\r\n") == {}
    assert parse_qeng("") == {}


def test_an_unknown_technology_records_only_what_it_is():
    """The rig's modem is LTE-only, so a 5G line would mean the hardware
    changed. Record the fact and leave the numbers alone rather than decoding
    a layout nobody has seen."""
    fields = parse_qeng('+QENG: "servingcell","NOCONN","NR5G-SA",123,456')
    assert fields == {"tech": "NR5G-SA"}


def test_every_column_the_sample_table_wants_is_produced():
    """The worker copies a fixed set of keys into each sample row; a rename on
    either side would silently write NULLs for a whole survey."""
    fields = parse_qeng(REAL_QENG)
    for column in ("rsrp", "rsrq", "sinr", "rssi", "band", "cell_id", "tech"):
        assert fields.get(column) is not None, column
