import json
import pytest
from pathlib import Path


@pytest.fixture
def tmp_base(tmp_path, monkeypatch):
    import config
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config, "BASE", tmp_path)
    monkeypatch.setattr(config, "DATA", data_dir)
    monkeypatch.setattr(config, "CONFIGS", data_dir / "configs")
    return data_dir


def test_load_sidebar_missing_returns_empty(tmp_base):
    from config import load_sidebar
    assert load_sidebar() == {"sections": []}


def test_load_config_missing_returns_defaults(tmp_base):
    from config import load_config
    result = load_config("test")
    assert result["sections"] == []
    assert result["customization_prompt"] == ""
    assert result["last_run"] is None


def test_load_sidebar_reads_file(tmp_base):
    from config import load_sidebar
    data = {
        "seeded_at": "2026-06-10T00:00:00Z",
        "sections": [{"name": "Critical", "channels": [{"id": "C1", "name": "incidents"}]}],
    }
    (tmp_base / "sidebar.json").write_text(json.dumps(data))
    assert load_sidebar()["sections"][0]["name"] == "Critical"


def test_save_and_reload_config(tmp_base):
    from config import save_config, load_config
    data = {"customization_prompt": "test", "sections": [], "last_run": None}
    save_config("test", data)
    assert load_config("test")["customization_prompt"] == "test"


def test_merge_new_channels_returns_unassigned(tmp_base):
    from config import merge_new_channels
    sidebar = {"sections": [{"name": "S1", "channels": [{"id": "C1", "name": "new"}]}]}
    cfg = {"sections": [{"name": "S1", "depth": "high", "channels": []}]}
    unassigned = merge_new_channels(sidebar, cfg)
    assert {"id": "C1", "name": "new"} in unassigned


def test_merge_new_channels_excludes_assigned(tmp_base):
    from config import merge_new_channels
    sidebar = {"sections": [{"name": "S1", "channels": [{"id": "C1", "name": "incidents"}]}]}
    cfg = {"sections": [{"name": "S1", "depth": "high", "channels": [{"id": "C1", "name": "incidents"}]}]}
    assert merge_new_channels(sidebar, cfg) == []


def test_load_config_returns_independent_defaults(tmp_base):
    """Each call to load_config for a missing file returns a fresh dict."""
    from config import load_config
    a = load_config("missing")
    b = load_config("missing")
    a["sections"].append({"name": "x"})
    assert b["sections"] == [], "mutating one default must not affect the next call"


def test_save_config_creates_missing_directory(tmp_path, monkeypatch):
    import config
    sub = tmp_path / "nonexistent"
    monkeypatch.setattr(config, "BASE", sub)
    monkeypatch.setattr(config, "DATA", sub / "data")
    monkeypatch.setattr(config, "CONFIGS", sub / "data" / "configs")
    data = {"customization_prompt": "x", "sections": [], "last_run": None}
    from config import save_config
    save_config("test", data)
    assert (sub / "data" / "configs" / "test" / "config.json").exists()
