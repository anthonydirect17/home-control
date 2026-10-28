import pytest

from home_control import secrets


@pytest.fixture(autouse=True)
def _isolated_secrets(tmp_path, monkeypatch):
    """Every test gets its own secrets folder, so no test reads or writes the real data/ folder."""
    monkeypatch.setattr(secrets, "_data_dir", str(tmp_path / "secrets-data"))
    for spec in secrets.SECRETS.values():
        monkeypatch.delenv(spec["env"], raising=False)
