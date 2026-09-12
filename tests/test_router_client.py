import time

import pytest

from viabot_survey.router_client import (AtOverSshRouterClient, LuciRouterClient,
                                         NullRouterClient, UbusRouterClient,
                                         _parse_at_output, at_stream_script,
                                         build_client, normalize_signal,
                                         parse_qeng)


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


# ---- the AT-over-SSH stream ------------------------------------------------

def test_the_ssh_command_carries_the_options_the_router_needs():
    """The router's Dropbear offers only an RSA host key, which modern OpenSSH
    refuses by default. Without these two the connection dies before it ever
    asks for a password — which is exactly how it failed by hand."""
    argv = AtOverSshRouterClient("192.168.1.1").build_command()
    assert "HostKeyAlgorithms=+ssh-rsa" in argv
    assert "PubkeyAcceptedAlgorithms=+ssh-rsa" in argv
    assert argv[-2] == "root@192.168.1.1"


def test_the_password_never_reaches_the_command_line():
    """sshpass -p would put it in argv, where anyone on the Pi could read it
    out of ps for the length of the survey."""
    client = AtOverSshRouterClient("192.168.1.1", password="hunter2")
    assert not any("hunter2" in arg for arg in client.build_command())
    assert client.build_env()["SSHPASS"] == "hunter2"


def test_no_password_means_key_authentication_and_no_sshpass():
    argv = AtOverSshRouterClient("192.168.1.1").build_command()
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv


def test_the_stream_script_sweeps_stale_readers_first():
    """A cat left over from a dropped connection steals characters from the new
    one, and the symptom is torn readings rather than silence."""
    script = at_stream_script("/dev/ttyUSB2")
    assert script.index("kill") < script.index("cat /dev/ttyUSB2 &")


def test_the_stream_script_never_polls_faster_than_once_a_second():
    assert "sleep 1" in at_stream_script(interval_s=0.05)


class _LocalAtClient(AtOverSshRouterClient):
    """The real client with the SSH hop removed, so the streaming script can be
    exercised against a pseudo-terminal standing in for the modem."""

    def build_command(self):
        return ["sh", "-c", at_stream_script(self.device, self.command, self.interval_s)]

    def build_env(self):
        return None

    def missing_tool(self):
        return None            # no ssh involved: the script runs here


def test_a_reading_flows_from_the_port_all_the_way_to_fetch():
    """End to end without a router: a pty plays the modem, and the same shell
    the rig sends to the router has to poke it, read the reply back, and get it
    parsed. Shell this fiddly is not worth trusting unexercised."""
    import os
    import select

    master, slave = os.openpty()
    client = _LocalAtClient("localhost", device=os.ttyname(slave), interval_s=1.0)
    try:
        with pytest.raises(RuntimeError):
            client.fetch()          # starts the stream; nothing has arrived yet

        # Wait for the script to poke the modem, then answer the way one does.
        deadline = time.time() + 15
        asked = False
        while time.time() < deadline and not asked:
            if select.select([master], [], [], 1.0)[0]:
                asked = b"QENG" in os.read(master, 4096)
        assert asked, "the script never sent the AT command"
        os.write(master, REAL_QENG.split("\r\n", 1)[1].encode())

        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                fields = client.fetch()
                break
            except RuntimeError:
                time.sleep(0.2)
        else:
            raise AssertionError("the reading never reached fetch()")

        assert fields["rsrp"] == -104.0
        assert fields["cell_id"] == "1452808"
    finally:
        client.close()
        os.close(master)
        os.close(slave)


def test_a_stale_stream_raises_rather_than_repeating_the_last_reading():
    """The worker shows 'degraded' off this exception. Serving an hour-old
    RSRP as if it were current would put confident wrong numbers on samples."""
    client = AtOverSshRouterClient("192.168.1.1")
    client._latest = {"rsrp": -104.0}
    client._latest_ts = time.time() - 3600
    client._proc = type("P", (), {"poll": staticmethod(lambda: None)})()
    with pytest.raises(RuntimeError):
        client.fetch()


def test_build_client_knows_the_at_backend():
    client = build_client({"client": "at_ssh"}, "192.168.1.1")
    assert isinstance(client, AtOverSshRouterClient)


def test_a_refused_login_is_not_retried_every_poll():
    """ssh exits immediately on a bad password. Without a floor the rig would
    reconnect twice a second for the length of a survey, which is how you get
    throttled by a router that counts failed logins."""
    client = _LocalAtClient("localhost", device="/dev/null", interval_s=1.0)
    client._last_start = time.monotonic()
    with pytest.raises(RuntimeError, match="reconnect"):
        client.fetch()
