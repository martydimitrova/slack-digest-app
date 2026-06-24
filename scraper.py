"""
Scrape Slack sidebar sections and channels from the web app.

Usage:
    python scraper.py --workspace <TXXXXXXXX>
    python scraper.py --workspace <TXXXXXXXX> --reset-session

First run: a browser opens so you can log in to Slack. Subsequent runs reuse
the saved session stored in data/.playwright-session/.
"""

import argparse
import asyncio
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent
DATA = BASE / "data"
SESSION_DIR = DATA / ".playwright-session"

# Channel types to include. "im" (1:1 DMs) are excluded.
INCLUDE_TYPES = {"channel", "private", "mpim"}

# data-qa values on built-in Slack section headings that we always skip.
BUILTIN_SECTION_QA = {"recent_apps"}


async def _wait_for_sidebar(context, timeout_ms: int = 120_000):
    """Return the first page in the context that has the pagesDivider rendered.

    Enterprise SSO opens auth tabs that navigate through dd.enterprise.slack.com
    and then close, leaving the original (or a sibling) tab at the Slack client.
    A 5-second poll loop re-watches any page whose previous watch ended so we
    don't miss the client page after the SSO redirect completes.
    """
    SELECTOR = "[data-item-key='pagesDivider']"
    loop = asyncio.get_event_loop()
    ready: asyncio.Future = loop.create_future()
    watching: set[int] = set()  # id(page) currently being watched

    async def watch_one(pg):
        watching.add(id(pg))
        print(f"  Watching: {pg.url or '(navigating...)'}", flush=True)
        try:
            await pg.wait_for_selector(SELECTOR, timeout=30_000)
            if not ready.done():
                print(f"  pagesDivider found on: {pg.url}", flush=True)
                ready.set_result(pg)
        except Exception as exc:
            if not ready.done():
                print(f"  No sidebar yet on {pg.url}: {type(exc).__name__}", flush=True)
        finally:
            watching.discard(id(pg))

    def start_watching(pg):
        if id(pg) not in watching and not ready.done():
            asyncio.ensure_future(watch_one(pg))

    def on_new_page(pg):
        print(f"  New tab opened: {pg.url or '(navigating...)'}", flush=True)
        start_watching(pg)

    async def poll_pages():
        """Re-watch pages every 5 s — picks up tabs that navigated to Slack after SSO."""
        while not ready.done():
            await asyncio.sleep(5)
            for pg in context.pages:
                start_watching(pg)

    for pg in context.pages:
        start_watching(pg)
    context.on("page", on_new_page)
    poll_task = asyncio.ensure_future(poll_pages())
    try:
        return await asyncio.wait_for(ready, timeout=timeout_ms / 1000)
    finally:
        context.remove_listener("page", on_new_page)
        poll_task.cancel()


async def scrape(workspace: str) -> None:
    from playwright.async_api import async_playwright

    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    # Delete Chromium cache dirs before launching so Slack can't serve stale data
    # from Service Worker caches or HTTP disk cache. Auth lives in Cookies/Local
    # Storage, which are not touched here.
    # Delete Chromium storage dirs that could serve stale Slack state.
    # IndexedDB is where Slack persists its channel/workspace model between sessions —
    # without clearing it, Slack restores old state even after a fresh HTTP load.
    # Auth lives in Cookies and is not touched.
    # Clear all Chromium storage that could hold stale Slack state.
    # Cookies are intentionally excluded — they hold the auth session.
    for cache_subdir in ("Cache", "Code Cache", "Service Worker", "IndexedDB",
                         "Local Storage", "Session Storage"):
        p = SESSION_DIR / "Default" / cache_subdir
        if p.exists():
            shutil.rmtree(p)
            print(f"Cleared: {cache_subdir}", flush=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=False,
            args=["--window-size=1280,900"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        url = f"https://app.slack.com/client/{workspace}" if workspace else "https://app.slack.com"
        print(f"Navigating to {url}...")
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)

        # Login may open the workspace in a new tab — watch all pages.
        print("Waiting for Slack to load (log in if prompted)...")
        page = await _wait_for_sidebar(context, timeout_ms=120_000)
        print(f"Sidebar loaded on: {page.url}", flush=True)

        import re

        def parse_top(style: str) -> int:
            m = re.search(r"top:\s*(\d+)px", style or "")
            return int(m.group(1)) if m else 0

        print("Reading pagesDivider position...", flush=True)
        divider = await page.query_selector("[data-item-key='pagesDivider']")
        if not divider:
            raise RuntimeError(
                "pagesDivider not found. The sidebar may not have loaded fully."
            )
        divider_top = parse_top(await divider.get_attribute("style") or "")
        if divider_top == 0:
            # Not fatal — we no longer use divider_top to filter sections.
            print(
                "  Warning: pagesDivider has no 'top' style — virtualizer layout "
                "may differ. Continuing anyway.",
                flush=True,
            )
        else:
            print(f"  pagesDivider at top={divider_top}px", flush=True)

        # State shared between the initial expand and the scroll loop.
        section_names: dict[str, str] = {}  # section_id -> display name
        # Keyed by (section_id, channel_id) so a channel that appears in multiple
        # sections (e.g. "Starred" + its home section) is counted in each.
        channel_appearances: dict[tuple[str, str], str] = {}  # (sid, cid) -> name
        expanded_ids: set[str] = set()  # guard against re-expanding the same heading

        # ── helpers ────────────────────────────────────────────────────────────

        async def harvest_visible() -> None:
            """Harvest section headings and channel items currently in the DOM."""
            # Section headings — no position filter; we rely on BUILTIN_SECTION_QA
            # and the final "must have channels" guard to exclude built-ins.
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
                    print(f"  Section: {name!r}", flush=True)

            # Channel items
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
                name_el = await el.query_selector(".p-channel_sidebar__name")
                cname = (await name_el.inner_text()).strip() if name_el else cid
                channel_appearances[key] = cname

        async def expand_visible_collapsed() -> int:
            """Click any collapsed section headings currently in the DOM.
            Returns the number of sections newly expanded."""
            els = await page.query_selector_all("[data-qa-channel-section-collapsed='true']")
            count = 0
            for el in els:
                hid = await el.get_attribute("id") or ""
                # Only deduplicate when we have a stable ID; empty-ID elements are
                # always retried (they're already expanded if no longer matched).
                if hid and hid in expanded_ids:
                    continue
                try:
                    await el.click()
                    await page.wait_for_timeout(120)
                    if hid:
                        expanded_ids.add(hid)
                    count += 1
                except Exception as exc:
                    print(f"  Warning: could not expand section {hid!r}: {exc}", flush=True)
            return count

        # ── initial expand of sections visible at page-load ───────────────────

        initially_collapsed = await page.query_selector_all(
            "[data-qa-channel-section-collapsed='true']"
        )
        if initially_collapsed:
            print(f"Expanding {len(initially_collapsed)} collapsed section(s) visible at load...", flush=True)
            for el in initially_collapsed:
                hid = await el.get_attribute("id") or ""
                try:
                    await el.click()
                    await page.wait_for_timeout(150)
                    if hid:
                        expanded_ids.add(hid)
                except Exception as exc:
                    print(f"  Warning: {exc}", flush=True)
            # Wait for the DOM to settle (remaining collapsed → 0, or timeout).
            try:
                await page.wait_for_function(
                    "() => !document.querySelector(\"[data-qa-channel-section-collapsed='true']\")",
                    timeout=8_000,
                )
            except Exception:
                await page.wait_for_timeout(600)
        else:
            print("All sections already expanded (at load).", flush=True)

        # ── scroll loop: collect sections + channels, expand any stragglers ───
        #
        # Sections and channels live in a virtual list — only items near the viewport
        # are in the DOM.  We scroll top-to-bottom up to 3 times, harvesting after
        # each step.  We stop as soon as a full pass yields nothing new.

        sidebar_el = await page.query_selector("[data-qa='slack_kit_scrollbar']")
        if not sidebar_el:
            print(
                "  Warning: sidebar scrollbar element not found; "
                "single-pass harvest only.",
                flush=True,
            )

        print("Collecting sections and channels by scrolling sidebar...", flush=True)

        for pass_num in range(1, 4):
            prev_sec = len(section_names)
            prev_ch = len(channel_appearances)

            if sidebar_el:
                await page.evaluate("el => { el.scrollTop = 0; }", sidebar_el)
                await page.wait_for_timeout(400)

            # Expand + harvest at the top before we start scrolling.
            n = await expand_visible_collapsed()
            if n:
                await page.wait_for_timeout(250)
            await harvest_visible()

            if sidebar_el:
                at_bottom = False
                while not at_bottom:
                    at_bottom = await page.evaluate(
                        """el => {
                            const before = el.scrollTop;
                            el.scrollTop += Math.floor(el.clientHeight * 0.8);
                            return Math.abs(el.scrollTop - before) < 2;
                        }""",
                        sidebar_el,
                    )
                    await page.wait_for_timeout(250)
                    n = await expand_visible_collapsed()
                    if n:
                        # Give the DOM a moment to render the newly-expanded channels.
                        await page.wait_for_timeout(250)
                    await harvest_visible()

            new_sec = len(section_names) - prev_sec
            new_ch = len(channel_appearances) - prev_ch
            unique = len({cid for (_, cid) in channel_appearances})
            print(
                f"  Pass {pass_num}: +{new_sec} section(s), +{new_ch} pair(s) "
                f"[{len(section_names)} sections, {unique} unique channels, "
                f"{len(channel_appearances)} pairs total]",
                flush=True,
            )
            if new_sec == 0 and new_ch == 0 and pass_num > 1:
                print(f"  Stable — finished after {pass_num} pass(es).", flush=True)
                break

        unique_channels = len({cid for (_, cid) in channel_appearances})
        print(
            f"\nResult: {len(section_names)} sections, "
            f"{unique_channels} unique channels "
            f"({len(channel_appearances)} section-channel pairs).",
            flush=True,
        )

        print("Closing browser...", flush=True)
        await context.close()

    # ── post-browser: build sidebar.json ─────────────────────────────────────

    if not section_names:
        raise RuntimeError(
            "No sections found in the sidebar. "
            "Make sure you are logged in and the sidebar is fully loaded."
        )

    sections_channels: dict[str, list[dict]] = {sid: [] for sid in section_names}
    for (sid, cid), cname in channel_appearances.items():
        if sid in sections_channels:
            sections_channels[sid].append({"id": cid, "name": cname})

    unknown = {sid for (sid, _) in channel_appearances if sid not in sections_channels}
    if unknown:
        n = sum(1 for (sid, _) in channel_appearances if sid not in sections_channels)
        print(
            f"  Warning: {n} pair(s) had unrecognised section IDs {unknown} — dropped.",
            flush=True,
        )

    sections_out = [
        {"name": name, "channels": sections_channels.get(sid, [])}
        for sid, name in section_names.items()
        if sections_channels.get(sid)
    ]

    if not sections_out:
        raise RuntimeError(
            "Sections were found but no channels were extracted. "
            "Ensure all sections are expanded and try again."
        )

    total_pairs = sum(len(s["channels"]) for s in sections_out)
    output = {
        "seeded_at": datetime.now(timezone.utc).isoformat(),
        "workspace": workspace,
        "sections": sections_out,
    }

    DATA.mkdir(exist_ok=True)
    sidebar_path = DATA / "sidebar.json"
    sidebar_path.write_text(json.dumps(output, indent=2))
    print(f"\nWrote {len(sections_out)} sections, {total_pairs} channel-section pairs -> {sidebar_path}")
    print("Open the server with `python server.py` and create a config at /configs.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--workspace",
        default="",
        help="Slack workspace ID (starts with T). Omit to use the session default.",
    )
    parser.add_argument(
        "--reset-session",
        action="store_true",
        help="Delete saved session and log in fresh.",
    )
    args = parser.parse_args()

    if args.reset_session and SESSION_DIR.exists():
        import shutil
        shutil.rmtree(SESSION_DIR)
        print("Session cleared.")

    asyncio.run(scrape(args.workspace))
