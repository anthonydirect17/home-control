"""Secrets encrypted with DPAPI on Windows: round trip, migration, the check command, failed decryption.

DPAPI itself only exists on Windows. Here a stand-in (conftest.FakeDpapi) plays its part, and the
ctypes calls in dpapi.py are run against stand-ins for crypt32 and kernel32.
"""

import ctypes
import json
import sys

import pytest

from home_control import dpapi, secrets

VALUES = {
    "govee-api-key": "govee-key-123",
    "page-pin-hash": "scrypt$abc$def",
    "ring-refresh-token": "ring-token-xyz",
    "session-key": "c2Vzc2lvbi1rZXk=",
    "govee-desktop-guid": "guid-0000",
}


def file_text():
    with open(secrets.secrets_file(), encoding="utf-8") as f:
        return f.read()


def write_plain(values):
    path = secrets.secrets_file()
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({secrets.SECRETS[n]["target"]: v for n, v in values.items()}, f)


def run_cli(monkeypatch, *argv):
    """Runs the command line with --data pointing at this test's secrets folder."""
    from home_control import __main__ as cli
    monkeypatch.setattr(sys, "argv", ["home_control", "--data", secrets._data_dir, *argv])
    return cli.main()


# ---------- round trip ----------

@pytest.mark.parametrize("name", list(VALUES))
def test_every_save_is_encrypted_and_reads_back(fake_dpapi, name):
    secrets.save_secret_value(name, VALUES[name], {})
    stored = json.loads(file_text())[secrets.SECRETS[name]["target"]]
    assert stored.startswith("dpapi:") and VALUES[name] not in file_text()
    assert secrets.get_secret(name, {}) == (VALUES[name], secrets.secrets_file())


def test_ring_token_rotation_is_saved_encrypted(fake_dpapi, monkeypatch, tmp_path):
    from argparse import Namespace

    from home_control import __main__ as cli
    from home_control import ring
    from home_control.events import EventBus
    from home_control.store import Store
    monkeypatch.setattr(ring, "setup_problem", lambda node: None)
    bridge = cli.build_ring(Store(str(tmp_path / "settings.json")), Namespace(demo=False), EventBus())
    bridge.save_token("rotated-token-1")  # what the bridge does when Ring hands over a new token
    assert "rotated-token-1" not in file_text() and '"dpapi:' in file_text()
    assert bridge.get_token()[0] == "rotated-token-1"


def test_if_encryption_fails_nothing_is_written(fake_dpapi):
    secrets.save_secret_value("govee-api-key", "old-key", {})
    before = file_text()

    def broken(data):
        raise OSError(5, "Access is denied")
    fake_dpapi.protect = broken
    with pytest.raises(OSError):
        secrets.save_secret_value("govee-api-key", "new-key", {})
    assert file_text() == before and "new-key" not in file_text()


# ---------- migration ----------

def test_plain_values_are_encrypted_once(fake_dpapi):
    write_plain({"govee-api-key": VALUES["govee-api-key"], "page-pin-hash": VALUES["page-pin-hash"]})
    assert secrets.encrypt_saved_secrets() == ["govee-api-key", "page-pin-hash"]
    text = file_text()
    assert VALUES["govee-api-key"] not in text and VALUES["page-pin-hash"] not in text
    assert all(v.startswith("dpapi:") for v in json.loads(text).values())
    assert secrets.get_secret("govee-api-key", {})[0] == VALUES["govee-api-key"]
    assert secrets.encrypt_saved_secrets() == []  # nothing left to do


def test_plain_values_still_read_before_migration(fake_dpapi):
    write_plain({"govee-api-key": "legacy"})
    assert secrets.get_secret("govee-api-key", {})[0] == "legacy"


def test_migration_runs_at_start_and_logs_names_only(fake_dpapi, monkeypatch, tmp_path):
    from home_control import __main__ as cli
    write_plain({"govee-api-key": VALUES["govee-api-key"], "ring-refresh-token": VALUES["ring-refresh-token"]})
    monkeypatch.setattr(cli, "serve", lambda args, store: 0)
    log = tmp_path / "logs" / "hc.log"
    stdout, stderr = sys.stdout, sys.stderr
    try:
        assert run_cli(monkeypatch, "--log-file", str(log)) == 0
        sys.stdout.close()
    finally:
        sys.stdout, sys.stderr = stdout, stderr
    text = log.read_text()
    assert "Encrypted 2 saved secret(s) with Windows DPAPI: govee-api-key, ring-refresh-token" in text
    assert VALUES["govee-api-key"] not in text and VALUES["ring-refresh-token"] not in text
    assert VALUES["govee-api-key"] not in file_text()


def test_start_survives_an_encryption_failure(fake_dpapi, monkeypatch, tmp_path, capsys):
    from home_control import __main__ as cli
    write_plain({"govee-api-key": VALUES["govee-api-key"]})

    def broken(data):
        raise OSError(5, "Access is denied")
    fake_dpapi.protect = broken
    monkeypatch.setattr(cli, "serve", lambda args, store: 0)
    assert run_cli(monkeypatch) == 0
    out = capsys.readouterr().out
    assert "Couldn't encrypt the saved secrets" in out and VALUES["govee-api-key"] not in out


# ---------- the check command ----------

def test_check_secrets_lists_names_and_state_never_values(fake_dpapi, monkeypatch, tmp_path, capsys):
    secrets.save_secret_value("govee-api-key", VALUES["govee-api-key"], {})
    secrets.save_secret_value("ring-refresh-token", VALUES["ring-refresh-token"], {})
    assert run_cli(monkeypatch, "--check-secrets") == 0
    out = capsys.readouterr().out
    assert "  govee-api-key: encrypted" in out and "  ring-refresh-token: encrypted" in out
    assert "  page-pin-hash: not saved" in out
    assert not any(v in out for v in VALUES.values())


def test_check_secrets_fails_on_a_plain_value(fake_dpapi, monkeypatch, tmp_path, capsys):
    write_plain({"page-pin-hash": VALUES["page-pin-hash"]})
    assert run_cli(monkeypatch, "--check-secrets") == 1
    out = capsys.readouterr().out
    assert "  page-pin-hash: NOT encrypted" in out and VALUES["page-pin-hash"] not in out


def test_check_secrets_names_other_entries_and_env_vars(fake_dpapi, monkeypatch, tmp_path, capsys):
    secrets.save_secret_value("govee-api-key", "k", {})
    data = json.loads(file_text())
    data["something/else"] = secrets._conceal("x")
    secrets._write_file_all(data)
    monkeypatch.setenv("RING_REFRESH_TOKEN", "from-env")
    run_cli(monkeypatch, "--check-secrets")
    out = capsys.readouterr().out
    assert "  something/else: encrypted" in out
    assert "  ring-refresh-token: from the RING_REFRESH_TOKEN environment variable (not on disk)" in out
    assert "from-env" not in out


def test_check_secrets_reports_a_plain_copy_on_disk_even_with_an_env_var(fake_dpapi, monkeypatch, capsys):
    write_plain({"govee-api-key": "plain-on-disk"})
    monkeypatch.setenv("GOVEE_API_KEY", "from-env")
    assert run_cli(monkeypatch, "--check-secrets") == 1
    out = capsys.readouterr().out
    assert "  govee-api-key: NOT encrypted (it will be encrypted at the next start); the GOVEE_API_KEY" in out
    assert "plain-on-disk" not in out and "from-env" not in out


# ---------- failed decryption ----------

def test_value_from_another_account_counts_as_missing(fake_dpapi, monkeypatch, tmp_path, capsys):
    secrets.save_secret_value("govee-api-key", VALUES["govee-api-key"], {})
    secrets.save_secret_value("page-pin-hash", VALUES["page-pin-hash"], {})
    stored = json.loads(file_text())[secrets.SECRETS["govee-api-key"]["target"]]
    fake_dpapi.account = b"someone-else"

    value, reason = secrets.get_secret("govee-api-key", {})
    assert value is None
    assert "can't decrypt it" in reason and "--set-govee-key" in reason
    assert stored not in reason and stored[6:20] not in reason

    assert run_cli(monkeypatch, "--check-secrets") == 1
    out = capsys.readouterr().out
    assert "  govee-api-key: encrypted, but this Windows account can't decrypt it" in out
    assert stored not in out

    # The page stays locked, and says why.
    from home_control import __main__ as cli
    from home_control.store import Store
    gate = cli.build_gate(Store(str(tmp_path / "settings.json")), demo=False)
    assert not gate.pin_set
    assert "can't decrypt it" in capsys.readouterr().out


def test_garbled_value_counts_as_missing(fake_dpapi):
    data = {secrets.SECRETS["govee-api-key"]["target"]: "dpapi:not base64!!"}
    secrets._write_file_all(data)
    value, reason = secrets.get_secret("govee-api-key", {})
    assert value is None and "can't decrypt it" in reason and "not base64" not in reason


# ---------- other systems keep today's behaviour ----------

def test_without_dpapi_values_stay_plain_in_the_locked_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(secrets, "_cipher", lambda: None)  # as on any system but Windows
    secrets.save_secret_value("govee-api-key", "plain-key", {})
    assert json.loads(file_text())[secrets.SECRETS["govee-api-key"]["target"]] == "plain-key"
    assert secrets.encrypt_saved_secrets() == []
    assert run_cli(monkeypatch, "--check-secrets") == 0
    assert "govee-api-key: not encrypted (DPAPI is Windows only" in capsys.readouterr().out


def test_without_dpapi_an_encrypted_value_counts_as_missing(monkeypatch):
    monkeypatch.setattr(secrets, "_cipher", lambda: None)
    secrets._write_file_all({secrets.SECRETS["govee-api-key"]["target"]: "dpapi:AAAA"})
    value, reason = secrets.get_secret("govee-api-key", {})
    assert value is None and "can't be read on this system" in reason


# ---------- the ctypes calls in dpapi.py ----------

class FakeCrypt32:
    """Plays crypt32.dll: reads the input blobs the way Windows does and hands back a buffer."""

    def __init__(self):
        self.kept, self.calls = [], []

    @staticmethod
    def _read(p):
        return ctypes.string_at(p.contents.pbData, p.contents.cbData)

    def _give(self, p_out, data):
        buf = ctypes.create_string_buffer(data, len(data))
        self.kept.append(buf)
        p_out.contents.cbData = len(data)
        p_out.contents.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))

    def CryptProtectData(self, p_in, descr, p_entropy, reserved, prompt, flags, p_out):
        entropy = self._read(p_entropy)
        self.calls.append(("protect", descr, flags, entropy, reserved, prompt))
        self._give(p_out, b"ENC" + entropy + b"|" + self._read(p_in)[::-1])
        return 1

    def CryptUnprotectData(self, p_in, descr, p_entropy, reserved, prompt, flags, p_out):
        self.calls.append(("unprotect", descr, flags, self._read(p_entropy), reserved, prompt))
        prefix = b"ENC" + self._read(p_entropy) + b"|"
        data = self._read(p_in)
        if not data.startswith(prefix):
            return 0
        self._give(p_out, data[len(prefix):][::-1])
        return 1


class FakeKernel32:
    def __init__(self):
        self.freed = []

    def LocalFree(self, p):
        self.freed.append(p.value)


def test_dpapi_calls_round_trip_and_free_their_buffers():
    libs = (FakeCrypt32(), FakeKernel32())
    sealed = dpapi.protect(b"top secret \x00 bytes", libs=libs)
    assert sealed != b"top secret \x00 bytes"
    assert dpapi.unprotect(sealed, libs=libs) == b"top secret \x00 bytes"
    crypt32, kernel32 = libs
    assert [c[0] for c in crypt32.calls] == ["protect", "unprotect"]
    assert crypt32.calls[0][1:] == ("home-control", dpapi.CRYPTPROTECT_UI_FORBIDDEN, dpapi.ENTROPY, None, None)
    assert crypt32.calls[1][2:] == (dpapi.CRYPTPROTECT_UI_FORBIDDEN, dpapi.ENTROPY, None, None)
    assert len(kernel32.freed) == 2 and all(kernel32.freed)


def test_dpapi_failure_raises_and_frees_nothing():
    libs = (FakeCrypt32(), FakeKernel32())
    sealed = dpapi.protect(b"x", libs=libs)
    with pytest.raises(OSError, match="couldn't decrypt"):
        dpapi.unprotect(sealed, entropy=b"wrong", libs=libs)
    assert len(libs[1].freed) == 1  # only the successful protect's buffer


def test_dpapi_empty_value():
    libs = (FakeCrypt32(), FakeKernel32())
    assert dpapi.unprotect(dpapi.protect(b"", libs=libs), libs=libs) == b""


# ---------- the log rotates while running ----------

def test_log_rotates_while_running(tmp_path, monkeypatch):
    from home_control import __main__ as cli
    monkeypatch.setattr(cli, "LOG_MAX_BYTES", 200)
    path = tmp_path / "hc.log"
    stdout, stderr = sys.stdout, sys.stderr
    try:
        cli.open_log(str(path))
        for i in range(60):
            print(f"line {i:02d} " + "x" * 40)
        sys.stdout.close()
    finally:
        sys.stdout, sys.stderr = stdout, stderr
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["hc.log", "hc.log.1", "hc.log.2", "hc.log.3"]
    assert all(p.stat().st_size <= 200 + 60 for p in tmp_path.iterdir())
    assert "line 59" in path.read_text()  # the newest lines are in the current file
