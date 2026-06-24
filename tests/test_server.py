import json
import pytest
from pathlib import Path


@pytest.fixture
def tmp_base(tmp_path, monkeypatch):
    import config
    import server
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "digests").mkdir()
    monkeypatch.setattr(config, "BASE", tmp_path)
    monkeypatch.setattr(config, "DATA", data_dir)
    monkeypatch.setattr(config, "CONFIGS", data_dir / "configs")
    monkeypatch.setattr(server, "BASE", tmp_path)
    monkeypatch.setattr(server, "DATA", data_dir)
    return data_dir


@pytest.fixture
def client(tmp_base):
    from server import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _make_config(tmp_base, name="daily", sections=None):
    d = tmp_base / "configs" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps({"sections": sections or [], "last_run": None}))
    return d


def test_root_no_digests_returns_200(client):
    assert client.get("/").status_code == 200


def test_root_with_digest_shows_library(client, tmp_base):
    """/ always shows the library view (200) — it never redirects."""
    cfg_dir = _make_config(tmp_base)
    run_id = "2026-06-09T09-00--2026-06-10T09-00"
    digest_dir = tmp_base / "digests" / "daily"
    digest_dir.mkdir(parents=True, exist_ok=True)
    (digest_dir / f"{run_id}.json").write_text(json.dumps({
        "run_id": run_id, "generated_at": "", "window_start": "",
        "window_end": "", "sections": []
    }))
    r = client.get("/")
    assert r.status_code == 200
    assert run_id.encode() in r.data


def test_digest_run_not_found_returns_404(client, tmp_base):
    _make_config(tmp_base)
    (tmp_base / "digests" / "daily").mkdir(parents=True, exist_ok=True)
    assert client.get("/digests/daily/nonexistent").status_code == 404


def test_digest_run_returns_200(client, tmp_base):
    run_id = "2026-06-09T09-00--2026-06-10T09-00"
    _make_config(tmp_base)
    digest_dir = tmp_base / "digests" / "daily"
    digest_dir.mkdir(parents=True, exist_ok=True)
    detail = {
        "run_id": run_id, "generated_at": "2026-06-10T09:05:00Z",
        "window_start": "2026-06-09T09:00:00Z", "window_end": "2026-06-10T09:00:00Z",
        "sections": [{"name": "Critical", "depth": "high", "section_summary": "", "channels": []}]
    }
    (digest_dir / f"{run_id}.json").write_text(json.dumps(detail))
    assert client.get(f"/digests/daily/{run_id}").status_code == 200


def test_config_get_syncs_missing_sections_from_sidebar(client, tmp_base):
    """sidebar.json has 3 sections; config.json only has 1.
    After GET /config/<name> the missing 2 sections are added in sidebar order,
    and the existing section's user settings (depth, prompt) are preserved."""
    sidebar = {
        "sections": [
            {"name": "Alpha", "channels": [{"id": "C1", "name": "alpha-ch"}]},
            {"name": "Beta",  "channels": [{"id": "C2", "name": "beta-ch"}, {"id": "C3", "name": "beta-ch2"}]},
            {"name": "Gamma", "channels": [{"id": "C4", "name": "gamma-ch"}]},
        ]
    }
    config = {
        "last_run": None,
        "sections": [
            {"name": "Beta", "depth": "high", "customization_prompt": "focus on outages",
             "channels": [{"id": "C2", "name": "beta-ch"}]},
        ]
    }
    cfg_dir = _make_config(tmp_base)
    (tmp_base / "sidebar.json").write_text(json.dumps(sidebar))
    (cfg_dir / "config.json").write_text(json.dumps(config))

    r = client.get("/config/daily")
    assert r.status_code == 200

    saved = json.loads((cfg_dir / "config.json").read_text())
    names = [s["name"] for s in saved["sections"]]
    # All 3 sections present in sidebar order
    assert names == ["Alpha", "Beta", "Gamma"]
    # Existing section's settings preserved
    beta = next(s for s in saved["sections"] if s["name"] == "Beta")
    assert beta["depth"] == "high"
    assert beta["customization_prompt"] == "focus on outages"
    assert [ch["id"] for ch in beta["channels"]] == ["C2", "C3"]  # C3 newly discovered and appended
    # New sections seeded with the "skip" default
    alpha = next(s for s in saved["sections"] if s["name"] == "Alpha")
    assert alpha["depth"] == "skip"
    assert [ch["id"] for ch in alpha["channels"]] == ["C1"]


def test_config_post_saves_sections_preserves_last_run(client, tmp_base):
    cfg_dir = _make_config(tmp_base)
    existing = {"last_run": "2026-06-10T09:00:00Z", "sections": []}
    (cfg_dir / "config.json").write_text(json.dumps(existing))
    r = client.post("/config/daily", json={
        "sections": [{"name": "S1", "depth": "high", "customization_prompt": "focus on incidents",
                      "channels": [{"id": "C1", "name": "incidents"}]}]
    })
    assert r.status_code == 204
    saved = json.loads((cfg_dir / "config.json").read_text())
    assert saved["sections"][0]["name"] == "S1"
    assert saved["sections"][0]["customization_prompt"] == "focus on incidents"
    assert saved["last_run"] == "2026-06-10T09:00:00Z"


def test_digest_run_with_corrupt_json_returns_404(client, tmp_base):
    run_id = "2026-06-09T09-00--2026-06-10T09-00"
    _make_config(tmp_base)
    digest_dir = tmp_base / "digests" / "daily"
    digest_dir.mkdir(parents=True, exist_ok=True)
    (digest_dir / f"{run_id}.json").write_text("not valid json {{{")
    assert client.get(f"/digests/daily/{run_id}").status_code == 404


def test_scrape_status_idle_when_not_running(client, tmp_base):
    r = client.get("/scrape/status")
    assert r.status_code == 200
    data = r.get_json()
    assert data["status"] == "idle"
    assert data["lines"] == []
    assert data["has_diff"] is False


def test_scrape_status_returns_log_lines(client, tmp_base):
    (tmp_base / "scrape.status").write_text("running")
    (tmp_base / "scrape.log").write_text("line1\nline2\nline3\n")
    r = client.get("/scrape/status?from=1")
    assert r.status_code == 200
    data = r.get_json()
    assert data["lines"] == ["line2", "line3"]
    assert data["status"] == "running"


def test_scrape_dismiss_deletes_ai_diff(client, tmp_base):
    cfg_dir = _make_config(tmp_base)
    (cfg_dir / "ai-diff.json").write_text('{"summary": "test"}')
    r = client.post("/scrape/dismiss", json={"config_name": "daily"})
    assert r.status_code == 204
    assert not (cfg_dir / "ai-diff.json").exists()


def test_scrape_dismiss_noop_when_no_diff(client, tmp_base):
    r = client.post("/scrape/dismiss")
    assert r.status_code == 204


def test_scrape_start_no_workspace_returns_400(client, tmp_base):
    # sidebar.json with no workspace field
    (tmp_base / "sidebar.json").write_text('{"sections": []}')
    r = client.post("/scrape/start")
    assert r.status_code == 400
    assert "workspace" in r.get_json()["error"].lower()


def test_config_get_passes_workspace_to_template(client, tmp_base):
    cfg_dir = _make_config(tmp_base)
    sidebar = {"workspace": "T123", "sections": []}
    (tmp_base / "sidebar.json").write_text(json.dumps(sidebar))
    r = client.get("/config/daily")
    assert r.status_code == 200
    # The Refresh button appears when workspace is truthy
    assert b"Refresh from Slack" in r.data


def test_config_get_shows_ai_diff_panel(client, tmp_base):
    cfg_dir = _make_config(tmp_base)
    sidebar = {"workspace": "T123", "sections": []}
    (tmp_base / "sidebar.json").write_text(json.dumps(sidebar))
    ai_diff = {
        "summary": "New section added: Beta.",
        "renames": [],
        "new_section_suggestions": [],
        "has_changes": True,
    }
    (cfg_dir / "ai-diff.json").write_text(json.dumps(ai_diff))
    r = client.get("/config/daily")
    assert r.status_code == 200
    assert b"New section added: Beta." in r.data
    assert b"Sidebar refreshed" in r.data
