"""
Compute a structural diff between two sidebar.json snapshots and optionally
enrich it with a Claude-generated analysis (rename detection, depth hints,
plain-English summary).

Call flow:
  1. compute_diff(old, new)  → structural dict (no AI, no I/O)
  2. call_claude(old, new, diff, old_config_sections) → AI analysis dict
  3. merge both dicts into ai-diff.json for the server to surface in /config
"""

import json
import os


def compute_diff(old_sidebar: dict, new_sidebar: dict) -> dict:
    """Pure structural diff — no Claude, no I/O."""
    old_sec = {s["name"]: s for s in old_sidebar.get("sections", [])}
    new_sec = {s["name"]: s for s in new_sidebar.get("sections", [])}
    old_ids = {n: {ch["id"] for ch in s.get("channels", [])} for n, s in old_sec.items()}
    new_ids = {n: {ch["id"] for ch in s.get("channels", [])} for n, s in new_sec.items()}

    added_sections = [n for n in new_sec if n not in old_sec]
    removed_sections = [n for n in old_sec if n not in new_sec]

    channel_changes: dict[str, dict] = {}
    for name in new_sec:
        if name not in old_sec:
            continue
        added_ch = [ch for ch in new_sec[name].get("channels", []) if ch["id"] not in old_ids[name]]
        removed_ch = [ch for ch in old_sec[name].get("channels", []) if ch["id"] not in new_ids[name]]
        if added_ch or removed_ch:
            channel_changes[name] = {"added": added_ch, "removed": removed_ch}

    return {
        "added_sections": added_sections,
        "removed_sections": removed_sections,
        "channel_changes": channel_changes,
        "has_changes": bool(added_sections or removed_sections or channel_changes),
    }


def _compact(sidebar: dict) -> list[dict]:
    """Strip IDs — keep only names, which are meaningful to Claude."""
    return [
        {"name": s["name"], "channels": [ch["name"] for ch in s.get("channels", [])]}
        for s in sidebar.get("sections", [])
    ]


def call_claude(
    old_sidebar: dict,
    new_sidebar: dict,
    diff: dict,
    old_config_sections: list[dict],
) -> dict:
    """
    Call Claude with compacted old+new sidebar to get:
      - rename suggestions (old section → new section, with confidence)
      - depth hints for new sections
      - plain-English summary

    Falls back to a heuristic summary if ANTHROPIC_API_KEY is not set or the
    call fails.  Old config settings are embedded in rename suggestions so the
    UI can offer one-click apply.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not diff["has_changes"]:
        return _fallback(diff)

    try:
        import anthropic
    except ImportError:
        return _fallback(diff)

    prompt = f"""You are helping a user understand what changed in their Slack sidebar between two scrapes.

Old sidebar (previous scrape):
{json.dumps(_compact(old_sidebar), indent=2)}

New sidebar (just scraped):
{json.dumps(_compact(new_sidebar), indent=2)}

Structural diff:
- Added sections: {diff["added_sections"]}
- Removed sections: {diff["removed_sections"]}
- Sections with channel changes: {list(diff["channel_changes"].keys())}

Analyze and return ONLY a JSON object with exactly these fields:
{{
  "renames": [
    {{"old_name": "...", "new_name": "...", "confidence": 0.0}}
  ],
  "new_section_suggestions": [
    {{"name": "...", "suggested_depth": "high|medium|low"}}
  ],
  "summary": "1-2 sentence plain-English description of what changed, friendly tone."
}}

Rules:
- renames: only include pairs where a removed section and an added section share
  significant channel overlap AND have plausibly related names. Confidence =
  fraction of old section's channels present in the new section. Only include
  if confidence >= 0.6.
- new_section_suggestions: for each section in added_sections that is NOT
  already explained by a rename. Use section name to guess depth:
  incidents/prod/oncall → high; projects/teams/eng → medium; social/news/spam/bots → low.
- summary: mention only meaningful changes (ignore if nothing changed).
Return ONLY the JSON, no other text."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        result = json.loads(msg.content[0].text)
    except Exception as exc:
        print(f"Claude API error during diff analysis: {exc}", flush=True)
        return _fallback(diff)

    # Embed old config settings into rename suggestions so the UI can apply them.
    old_cfg = {s["name"]: s for s in old_config_sections}
    for rename in result.get("renames") or []:
        old_settings = old_cfg.get(rename.get("old_name", ""), {})
        rename["old_depth"] = old_settings.get("depth", "medium")
        rename["old_prompt"] = old_settings.get("customization_prompt", "")

    return result


def _fallback(diff: dict) -> dict:
    parts = []
    if diff["added_sections"]:
        names = ", ".join(diff["added_sections"][:3])
        tail = f" and {len(diff['added_sections']) - 3} more" if len(diff["added_sections"]) > 3 else ""
        parts.append(f"{len(diff['added_sections'])} new section(s): {names}{tail}")
    if diff["removed_sections"]:
        parts.append(f"{len(diff['removed_sections'])} removed section(s)")
    if diff["channel_changes"]:
        parts.append(f"channel changes in {len(diff['channel_changes'])} section(s)")
    summary = ("Sidebar updated: " + "; ".join(parts) + ".") if parts else "Sidebar is up to date — no changes detected."
    return {"renames": [], "new_section_suggestions": [], "summary": summary}
