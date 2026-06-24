import json
import pytest

import diff as df


def _sidebar(sections):
    return {"sections": [
        {"name": name, "channels": [{"id": cid, "name": cid} for cid in cids]}
        for name, cids in sections.items()
    ]}


# ── compute_diff ──────────────────────────────────────────────────────────────

def test_no_changes_has_changes_false():
    s = _sidebar({"Alpha": ["C1", "C2"]})
    d = df.compute_diff(s, s)
    assert not d["has_changes"]
    assert d["added_sections"] == []
    assert d["removed_sections"] == []
    assert d["channel_changes"] == {}


def test_added_section_detected():
    old = _sidebar({"Alpha": ["C1"]})
    new = _sidebar({"Alpha": ["C1"], "Beta": ["C2"]})
    d = df.compute_diff(old, new)
    assert d["added_sections"] == ["Beta"]
    assert d["removed_sections"] == []
    assert d["has_changes"]


def test_removed_section_detected():
    old = _sidebar({"Alpha": ["C1"], "Beta": ["C2"]})
    new = _sidebar({"Alpha": ["C1"]})
    d = df.compute_diff(old, new)
    assert d["removed_sections"] == ["Beta"]
    assert d["added_sections"] == []
    assert d["has_changes"]


def test_channel_added_to_existing_section():
    old = _sidebar({"Alpha": ["C1"]})
    new = _sidebar({"Alpha": ["C1", "C2"]})
    d = df.compute_diff(old, new)
    assert d["added_sections"] == []
    assert "Alpha" in d["channel_changes"]
    assert any(ch["id"] == "C2" for ch in d["channel_changes"]["Alpha"]["added"])
    assert d["has_changes"]


def test_channel_removed_from_existing_section():
    old = _sidebar({"Alpha": ["C1", "C2"]})
    new = _sidebar({"Alpha": ["C1"]})
    d = df.compute_diff(old, new)
    assert "Alpha" in d["channel_changes"]
    assert any(ch["id"] == "C2" for ch in d["channel_changes"]["Alpha"]["removed"])


def test_rename_pattern_shows_as_add_and_remove():
    old = _sidebar({"Logs Engineering": ["C1", "C2", "C3"]})
    new = _sidebar({"Logs Teams": ["C1", "C2", "C3", "C4"]})
    d = df.compute_diff(old, new)
    assert "Logs Engineering" in d["removed_sections"]
    assert "Logs Teams" in d["added_sections"]


# ── _fallback ─────────────────────────────────────────────────────────────────

def test_fallback_no_changes():
    d = {"added_sections": [], "removed_sections": [], "channel_changes": {}, "has_changes": False}
    result = df._fallback(d)
    assert "up to date" in result["summary"].lower()
    assert result["renames"] == []
    assert result["new_section_suggestions"] == []


def test_fallback_with_changes_mentions_counts():
    d = {
        "added_sections": ["New1", "New2"],
        "removed_sections": ["Old1"],
        "channel_changes": {},
        "has_changes": True,
    }
    result = df._fallback(d)
    assert "2" in result["summary"]   # 2 new sections
    assert "1" in result["summary"]   # 1 removed section


# ── call_claude falls back gracefully without API key ─────────────────────────

def test_call_claude_no_api_key_returns_fallback(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    old = _sidebar({"Alpha": ["C1"]})
    new = _sidebar({"Alpha": ["C1"], "Beta": ["C2"]})
    d = df.compute_diff(old, new)
    result = df.call_claude(old, new, d, [])
    # Should return a valid fallback dict, not raise
    assert "summary" in result
    assert "renames" in result
    assert isinstance(result["renames"], list)
