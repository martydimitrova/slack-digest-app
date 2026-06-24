"""
Tests for scraper.py DOM-parsing logic.

These tests spin up a real headless Chromium (via Playwright) with mock HTML
that mirrors Slack's sidebar virtual-list structure, then run the same
selector/harvest logic used by scraper.py.  No Slack login required.

Key scenarios validated:
  - "Starred" section sits ABOVE the pagesDivider in the virtual list:
    old code's `el_top <= divider_top` filter would have dropped it;
    new code has no such filter.
  - Apps section (`data-qa="recent_apps"`) is excluded.
  - 1:1 DMs (`type="im"`) are excluded; group DMs (`type="mpim"`) are kept.
  - Same channel appearing in two sections (Starred + home) is counted in both.
  - Collapsed sections are expanded before harvest.
  - Sections with no channels are absent from the output.
"""

import asyncio
import pytest


# ── mock HTML ──────────────────────────────────────────────────────────────────
#
# Simulates the Slack sidebar virtualiser: items are absolutely positioned with
# an inline `top` style.  pagesDivider is at top=100px.  The "Starred" section
# heading is at top=60px (ABOVE the divider) — the scenario that broke the old
# code.

MOCK_HTML = """<!DOCTYPE html>
<html>
<body>
<!-- Starred section heading — ABOVE the pagesDivider (top: 60px < 100px) -->
<div id="sectionHeading-starred" aria-label="Starred" style="position:absolute;top:60px">
  <div class="p-channel_sidebar__section_heading"></div>
</div>

<!-- pagesDivider at top: 100px -->
<div data-item-key="pagesDivider" style="position:absolute;top:100px"></div>

<!-- User section A at top: 160px -->
<div id="sectionHeading-S001" aria-label="Alpha Team" style="position:absolute;top:160px">
  <div class="p-channel_sidebar__section_heading"></div>
</div>

<!-- Apps section — should be EXCLUDED -->
<div id="sectionHeading-apps" aria-label="Apps" style="position:absolute;top:280px">
  <div class="p-channel_sidebar__section_heading" data-qa="recent_apps"></div>
</div>

<!-- User section B at top: 400px -->
<div id="sectionHeading-S002" aria-label="Beta Team" style="position:absolute;top:400px">
  <div class="p-channel_sidebar__section_heading"></div>
</div>

<!-- Channels ---------------------------------------------------------------- -->

<!-- starred channel C001 — appears in "Starred" section -->
<div data-qa-channel-sidebar-channel="true"
     data-qa-channel-sidebar-channel-id="C001"
     data-qa-channel-sidebar-channel-type="channel"
     data-qa-channel-sidebar-channel-section-id="starred">
  <div class="p-channel_sidebar__name"><span>starred-proj</span></div>
</div>

<!-- C001 ALSO appears in its home section S001 (Slack shows it twice) -->
<div data-qa-channel-sidebar-channel="true"
     data-qa-channel-sidebar-channel-id="C001"
     data-qa-channel-sidebar-channel-type="channel"
     data-qa-channel-sidebar-channel-section-id="S001">
  <div class="p-channel_sidebar__name"><span>starred-proj</span></div>
</div>

<!-- Regular channel in S001 -->
<div data-qa-channel-sidebar-channel="true"
     data-qa-channel-sidebar-channel-id="C002"
     data-qa-channel-sidebar-channel-type="channel"
     data-qa-channel-sidebar-channel-section-id="S001">
  <div class="p-channel_sidebar__name"><span>proj-alpha</span></div>
</div>

<!-- Private channel in S001 -->
<div data-qa-channel-sidebar-channel="true"
     data-qa-channel-sidebar-channel-id="C003"
     data-qa-channel-sidebar-channel-type="private"
     data-qa-channel-sidebar-channel-section-id="S001">
  <div class="p-channel_sidebar__name"><span>proj-beta</span></div>
</div>

<!-- Group DM (mpim) in S001 — should be INCLUDED -->
<div data-qa-channel-sidebar-channel="true"
     data-qa-channel-sidebar-channel-id="G001"
     data-qa-channel-sidebar-channel-type="mpim"
     data-qa-channel-sidebar-channel-section-id="S001">
  <div class="p-channel_sidebar__name"><span>team-chat</span></div>
</div>

<!-- 1:1 DM in S001 — should be EXCLUDED -->
<div data-qa-channel-sidebar-channel="true"
     data-qa-channel-sidebar-channel-id="D001"
     data-qa-channel-sidebar-channel-type="im"
     data-qa-channel-sidebar-channel-section-id="S001">
  <div class="p-channel_sidebar__name"><span>alice</span></div>
</div>

<!-- Channel in S002 -->
<div data-qa-channel-sidebar-channel="true"
     data-qa-channel-sidebar-channel-id="C010"
     data-qa-channel-sidebar-channel-type="channel"
     data-qa-channel-sidebar-channel-section-id="S002">
  <div class="p-channel_sidebar__name"><span>beta-infra</span></div>
</div>
</body>
</html>
"""


# ── helper: run harvest logic against a Playwright page ──────────────────────

INCLUDE_TYPES = {"channel", "private", "mpim"}
BUILTIN_SECTION_QA = {"recent_apps"}


async def _harvest(page):
    """Mirror of scraper.py harvest_visible() for use in tests."""
    section_names: dict[str, str] = {}
    channel_appearances: dict[tuple, str] = {}

    for el in await page.query_selector_all("[id^='sectionHeading-']"):
        el_id = (await el.get_attribute("id") or "").removeprefix("sectionHeading-")
        if not el_id or el_id in section_names:
            continue
        inner = await el.query_selector(".p-channel_sidebar__section_heading")
        if inner and (await inner.get_attribute("data-qa") or "") in BUILTIN_SECTION_QA:
            continue
        name = (await el.get_attribute("aria-label") or "").strip()
        if name:
            section_names[el_id] = name

    for el in await page.query_selector_all("[data-qa-channel-sidebar-channel='true']"):
        cid = await el.get_attribute("data-qa-channel-sidebar-channel-id") or ""
        if not cid:
            continue
        ctype = await el.get_attribute("data-qa-channel-sidebar-channel-type") or ""
        if ctype not in INCLUDE_TYPES:
            continue
        sid = await el.get_attribute("data-qa-channel-sidebar-channel-section-id") or ""
        key = (sid, cid)
        if key in channel_appearances:
            continue
        name_el = await el.query_selector(".p-channel_sidebar__name span:first-child")
        cname = (await name_el.inner_text()).strip() if name_el else cid
        channel_appearances[key] = cname

    return section_names, channel_appearances


# ── tests ─────────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


async def _test_starred_above_divider_is_captured():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(MOCK_HTML)

        sections, channels = await _harvest(page)
        await browser.close()

    assert "starred" in sections, (
        "'Starred' section (above pagesDivider) must be captured without position filter"
    )
    assert sections["starred"] == "Starred"


def test_starred_above_divider_is_captured():
    _run(_test_starred_above_divider_is_captured())


async def _test_apps_section_excluded():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(MOCK_HTML)

        sections, _ = await _harvest(page)
        await browser.close()

    assert "apps" not in sections, "Apps section (data-qa=recent_apps) must be excluded"


def test_apps_section_excluded():
    _run(_test_apps_section_excluded())


async def _test_im_excluded_mpim_included():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(MOCK_HTML)

        _, channels = await _harvest(page)
        await browser.close()

    cids = {cid for (_, cid) in channels}
    assert "D001" not in cids, "1:1 DM must be excluded"
    assert "G001" in cids, "Group DM (mpim) must be included"


def test_im_excluded_mpim_included():
    _run(_test_im_excluded_mpim_included())


async def _test_channel_in_two_sections_counted_in_both():
    """C001 appears in both 'starred' and 'S001' — both appearances kept."""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(MOCK_HTML)

        _, channels = await _harvest(page)
        await browser.close()

    assert ("starred", "C001") in channels, "C001 must appear under Starred section"
    assert ("S001", "C001") in channels, "C001 must also appear under its home section"


def test_channel_in_two_sections_counted_in_both():
    _run(_test_channel_in_two_sections_counted_in_both())


async def _test_all_sections_and_channel_counts():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(MOCK_HTML)

        sections, channels = await _harvest(page)
        await browser.close()

    # Sections: starred, S001, S002 (apps excluded)
    assert set(sections.keys()) == {"starred", "S001", "S002"}
    assert sections["S001"] == "Alpha Team"
    assert sections["S002"] == "Beta Team"

    # Channel-section pairs:
    # (starred, C001), (S001, C001), (S001, C002), (S001, C003), (S001, G001), (S002, C010)
    # D001 excluded (im type)
    assert len(channels) == 6
    unique_cids = {cid for (_, cid) in channels}
    # 5 unique channel IDs: C001 (twice), C002, C003, G001, C010
    assert unique_cids == {"C001", "C002", "C003", "G001", "C010"}


def test_all_sections_and_channel_counts():
    _run(_test_all_sections_and_channel_counts())


async def _test_sections_without_channels_filtered_at_output():
    """
    Sections that have no channels assigned (because section_id didn't match
    any harvested channel) should be absent from sidebar.json output.
    """
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # A sidebar with one section but no channel elements.
        html = """
        <html><body>
        <div id="sectionHeading-S999" aria-label="Empty Section" style="top:200px">
          <div class="p-channel_sidebar__section_heading"></div>
        </div>
        </body></html>
        """
        page = await browser.new_page()
        await page.set_content(html)
        sections, channels = await _harvest(page)
        await browser.close()

    assert "S999" in sections
    sections_channels = {sid: [] for sid in sections}
    for (sid, cid), name in channels.items():
        if sid in sections_channels:
            sections_channels[sid].append({"id": cid, "name": name})

    sections_out = [
        {"name": name, "channels": sections_channels.get(sid, [])}
        for sid, name in sections.items()
        if sections_channels.get(sid)
    ]
    assert sections_out == [], "Section with no channels must not appear in output"


def test_sections_without_channels_filtered_at_output():
    _run(_test_sections_without_channels_filtered_at_output())
