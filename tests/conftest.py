import pytest

from home_control import secrets


@pytest.fixture(autouse=True)
def _isolated_secrets(tmp_path, monkeypatch):
    """Every test gets its own secrets folder, so no test reads or writes the real data/ folder."""
    monkeypatch.setattr(secrets, "_data_dir", str(tmp_path / "secrets-data"))
    for spec in secrets.SECRETS.values():
        monkeypatch.delenv(spec["env"], raising=False)


class FakeDpapi:
    """Stands in for home_control.dpapi: values only decrypt for the same 'account'."""

    def __init__(self):
        self.account = b"owner"

    def _prefix(self):
        return b"FAKE-DPAPI:" + self.account + b"|"

    def protect(self, data):
        return self._prefix() + bytes(b ^ 0x5A for b in data)

    def unprotect(self, blob):
        if not blob.startswith(self._prefix()):
            raise OSError(13, "The data is invalid")
        return bytes(b ^ 0x5A for b in blob[len(self._prefix()):])


@pytest.fixture
def fake_dpapi(monkeypatch):
    """Turns on encryption as on Windows, with a stand-in for DPAPI."""
    fake = FakeDpapi()
    monkeypatch.setattr(secrets, "_cipher", lambda: fake)
    return fake
