import pytest

from dgap import models


def _no_network(*args, **kwargs):
    raise AssertionError("a real provider call was attempted")


@pytest.fixture
def mock_env(monkeypatch):
    """Mock mode on, and any real provider call fails the test."""
    monkeypatch.setenv("DGAP_MOCK", "1")
    monkeypatch.setattr(models, "_call_litellm", _no_network)


@pytest.fixture
def real_env(monkeypatch):
    """Mock mode off, and any real provider call fails the test."""
    monkeypatch.delenv("DGAP_MOCK", raising=False)
    monkeypatch.setattr(models, "_call_litellm", _no_network)
