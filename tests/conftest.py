"""Global test safety: no test may touch the real home directory."""
import pytest


@pytest.fixture(autouse=True)
def fake_home(tmp_path_factory, monkeypatch):
    """Point HOME (and so `~` in any subprocess) at a throwaway directory."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    return home
