"""CLI defaults must never create or update the user's active Paper databases."""
import pytest


@pytest.fixture(autouse=True)
def isolated_default_data_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
