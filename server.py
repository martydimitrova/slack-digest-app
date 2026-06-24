import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from threading import Thread, Timer

from dotenv import load_dotenv
from flask import Flask, Response, make_response, render_template, request, stream_with_context

import config as cfg

BASE = Path(__file__).parent
DATA = BASE / "data"
app = Flask(__name__, template_folder=str(BASE / "templates"))


@app.template_filter("fmt_dur")
def fmt_dur(s: float) -> str:
    s = int(s)
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


@app.template_filter("fmt_dt")
def fmt_dt(s: str) -> str:
    """Format a run_id or ISO string to 'Jun 11 10:40'.

    Handles:
      - range run_ids:  '2026-06-11T10-40--2026-06-12T10-40' → 'Jun 11 10:40 → Jun 12 10:40'
      - single run_ids: '2026-06-11T14-00'                   → 'Jun 11 14:00'
      - ISO strings:    '2026-06-11T14:00:00+00:00'          → 'Jun 11 14:00'
    """
    if not s:
        return s
    if "--" in s:
        start, _, end = s.partition("--")
        return f"{fmt_dt(start)} → {fmt_dt(end)}"
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H-%M")
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return s
    return dt.strftime("%b %-d %H:%M")


_scrape_proc: subprocess.Popen | None = None
_digest_proc: subprocess.Popen | None = None
_scrape_lock = threading.Lock()
_digest_lock = threading.Lock()


load_dotenv(BASE / ".env", override=False)


# ── digest helpers ────────────────────────────────────────────────────────────

def _active_config_name() -> str:
    """Config name from query param → cookie → first available."""
    name = request.args.get("config") or request.cookies.get("digest_config", "")
    configs = cfg.list_configs()
    if name and name in configs:
        return name
    return configs[0] if configs else ""


def _parse_dt(raw: str | None, fallback: datetime) -> datetime:
    """Parse an ISO datetime string to an aware UTC datetime, falling back on error."""
    if not raw or not isinstance(raw, str):
        return fallback
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return fallback


def _resolve_run_path(config_name: str, run_id: str) -> Path:
    return cfg.digests_dir(config_name) / f"{run_id}.json"


def list_runs(config_name: str) -> list[str]:
    d = cfg.digests_dir(config_name)
    if not d.exists():
        return []
    files = [f for f in d.iterdir() if f.suffix == ".json"]
    return [f.stem for f in sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)]


def _latest_run(config_name: str) -> str | None:
    """Return the most-recent run stem without sorting the full list."""
    d = cfg.digests_dir(config_name)
    if not d.exists():
        return None
    files = [f for f in d.iterdir() if f.suffix == ".json"]
    if not files:
        return None
    return max(files, key=lambda f: f.stat().st_mtime).stem


def load_detail(config_name: str, run_id: str) -> dict | None:
    p = _resolve_run_path(config_name, run_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


# ── digest routes ─────────────────────────────────────────────────────────────

def _digest_render(detail, run_id, active):
    resp = make_response(render_template(
        "digest.html",
        detail=detail,
        run_id=run_id,
        runs=list_runs(active),
        active_config=active,
        all_configs=cfg.list_configs(),
        slack_channel=os.environ.get("SLACK_DIGEST_CHANNEL", ""),
    ))
    if active:
        resp.set_cookie("digest_config", active, max_age=365 * 24 * 3600)
    return resp


@app.route("/")
def index():
    active = _active_config_name()
    return _digest_render(None, None, active)


@app.route("/digests/<config_name>/<run_id>")
def digest_run(config_name: str, run_id: str):
    detail = load_detail(config_name, run_id)
    if detail is None:
        return "Run not found.", 404
    return _digest_render(detail, run_id, config_name)


@app.route("/digests/<config_name>/<run_id>", methods=["DELETE"])
def digest_delete(config_name: str, run_id: str):
    p = _resolve_run_path(config_name, run_id)
    if not p.exists():
        return {"error": "Not found."}, 404
    p.unlink()
    return "", 204


# ── config routes ─────────────────────────────────────────────────────────────

@app.route("/configs")
def configs_list():
    configs = cfg.list_configs()
    meta = []
    for name in configs:
        c = cfg.load_config(name)
        meta.append({
            "name": name,
            "section_count": sum(1 for s in c.get("sections", []) if s.get("depth") != "skip"),
            "last_run": _latest_run(name),
        })
    return render_template("configs.html", configs=meta)


@app.route("/configs", methods=["POST"])
def configs_create():
    data = request.get_json(force=True) or {}
    name = data.get("name", "").strip()
    if not name:
        return {"error": "Config name is required."}, 400
    if (cfg.CONFIGS / name).exists():
        return {"error": "Config already exists."}, 409
    cfg.create_config(name)
    return {"name": name}, 201


@app.route("/configs/<name>", methods=["DELETE"])
def configs_delete(name: str):
    d = cfg.CONFIGS / name
    if not d.exists():
        return {"error": "Not found."}, 404
    shutil.rmtree(d)
    return "", 204


def _sync_config(name: str, sidebar: dict, config: dict) -> tuple[dict, bool]:
    """Sync sidebar sections into config, returning (updated_config, dirty)."""
    sb_ch_map = {
        ch["id"]: ch.get("name", ch["id"])
        for sec in sidebar["sections"]
        for ch in sec.get("channels", [])
        if ch.get("id")
    }
    config_by_name = {s["name"]: s for s in config.get("sections", [])}
    synced = []
    dirty = False
    for sb_section in sidebar["sections"]:
        sec_name = sb_section["name"]
        sb_channels = sb_section.get("channels", [])
        if sec_name in config_by_name:
            existing = dict(config_by_name[sec_name])
            if "channel_ids" in existing and "channels" not in existing:
                existing["channels"] = [
                    {"id": cid, "name": sb_ch_map.get(cid, cid)}
                    for cid in existing.pop("channel_ids")
                ]
                dirty = True
            existing_ids = {ch["id"] for ch in existing.get("channels", [])}
            new_channels = [ch for ch in sb_channels if ch["id"] not in existing_ids]
            if new_channels:
                existing["channels"] = existing.get("channels", []) + new_channels
                dirty = True
            synced.append(existing)
        else:
            synced.append({"name": sec_name, "depth": "skip", "customization_prompt": "", "channels": sb_channels})
            dirty = True
    if [s["name"] for s in synced] != [s["name"] for s in config.get("sections", [])]:
        dirty = True
    config["sections"] = synced
    return config, dirty


@app.route("/config/<name>", methods=["GET"])
def config_get(name: str):
    if not (cfg.CONFIGS / name).exists():
        return "Config not found.", 404
    sidebar = cfg.load_sidebar()
    config = cfg.load_config(name)
    if sidebar.get("sections"):
        config, dirty = _sync_config(name, sidebar, config)
        if dirty:
            cfg.save_config(name, config)
    return render_template(
        "config.html",
        config_name=name,
        sidebar=sidebar,
        config=config,
        unassigned=cfg.merge_new_channels(sidebar, config),
        workspace=sidebar.get("workspace", ""),
        ai_diff=_load_ai_diff(name),
    )


@app.route("/config/<name>", methods=["POST"])
def config_save(name: str):
    if not (cfg.CONFIGS / name).exists():
        return "Config not found.", 404
    data = request.get_json(force=True)
    if data is None:
        return "Invalid JSON", 400
    current = cfg.load_config(name)
    current["sections"] = data.get("sections") or []
    cfg.save_config(name, current)
    return "", 204


# ── scrape routes ─────────────────────────────────────────────────────────────

@app.route("/scrape/start", methods=["POST"])
def scrape_start():
    global _scrape_proc

    status_path = DATA / "scrape.status"
    with _scrape_lock:
        if status_path.exists() and status_path.read_text().strip() == "running":
            if _scrape_proc and _scrape_proc.poll() is None:
                return {"error": "Scrape already running."}, 409

        sidebar = cfg.load_sidebar()
        workspace = sidebar.get("workspace", "")
        if not workspace:
            return {"error": "No workspace ID in sidebar.json. Run the initial scrape from the terminal first."}, 400

        # Back up sidebar for diff/rename detection.
        sidebar_path = DATA / "sidebar.json"
        if sidebar_path.exists():
            (DATA / "sidebar.prev.json").write_text(sidebar_path.read_text())

        log_path = DATA / "scrape.log"
        log_path.write_text("")
        (DATA / "ai-diff.json").unlink(missing_ok=True)
        status_path.write_text("running")

        body = request.get_json(force=True, silent=True) or {}
        reset_session = body.get("reset_session", False)
        config_name = body.get("config_name", "")

        # Snapshot this config's current state for AI diff after scrape
        if cfg.config_exists(config_name):
            config_path = cfg.config_dir(config_name) / "config.json"
            if config_path.exists():
                (cfg.config_dir(config_name) / "config.prev.json").write_text(config_path.read_text())

        scraper = BASE / "scraper.py"
        cmd = [sys.executable, str(scraper), "--workspace", workspace]
        if reset_session:
            cmd.append("--reset-session")
        try:
            _scrape_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as e:
            status_path.write_text("error")
            return {"error": f"Failed to start scraper: {e}"}, 500
        proc = _scrape_proc  # capture by value so _reader is unaffected by future assignments

    def _reader():
        with open(log_path, "a") as f:
            for line in proc.stdout:
                f.write(line)
                f.flush()
        ret = proc.wait()
        if ret == 0:
            _post_scrape(config_name)
            status_path.write_text("done")
        else:
            status_path.write_text("error")

    Thread(target=_reader, daemon=True).start()
    return {"status": "started"}, 202


@app.route("/scrape/status")
def scrape_status():
    status_path = DATA / "scrape.status"
    log_path = DATA / "scrape.log"

    status = status_path.read_text().strip() if status_path.exists() else "idle"

    from_line = request.args.get("from", 0, type=int)
    lines: list[str] = []
    if log_path.exists():
        all_lines = log_path.read_text().splitlines()
        lines = all_lines[from_line:]

    config_name = request.args.get("config", "")
    has_diff = cfg.config_exists(config_name) and (cfg.config_dir(config_name) / "ai-diff.json").exists()

    return {"status": status, "lines": lines, "has_diff": has_diff}


@app.route("/scrape/cancel", methods=["POST"])
def scrape_cancel():
    global _scrape_proc
    if _scrape_proc and _scrape_proc.poll() is None:
        _scrape_proc.terminate()
        try:
            _scrape_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _scrape_proc.kill()
        (DATA / "scrape.status").write_text("error")
    return "", 204


@app.route("/scrape/dismiss", methods=["POST"])
def scrape_dismiss():
    data = request.get_json(force=True, silent=True) or {}
    config_name = data.get("config_name", "")
    if cfg.config_exists(config_name):
        (cfg.config_dir(config_name) / "ai-diff.json").unlink(missing_ok=True)
    return "", 204


# ── digest routes ─────────────────────────────────────────────────────────────

def _build_digest_prompt(window_start: str, window_end: str, run_id: str, output_path: str, config_name: str = "", post_to_slack: bool = False) -> str:
    config = cfg.load_config(config_name) if config_name else {}

    active = [s for s in config.get("sections", []) if s.get("depth", "skip") != "skip"]

    sections_block = "\n\n".join(
        "\n".join([
            f"### {s['name']} [depth: {s['depth']}]",
            *(["Customisation: " + s["customization_prompt"]] if s.get("customization_prompt") else []),
            *[f"  - channel_id={ch['id']}  name=#{ch['name']}" for ch in s.get("channels", []) if ch.get("enabled", True)],
        ])
        for s in active
    ) or "(no active sections — all sections are set to skip)"

    digest_channel = os.environ.get("SLACK_DIGEST_CHANNEL", "")
    if digest_channel and post_to_slack:
        dm_line = (
            f"6. Post the full digest to Slack channel #{digest_channel} as a formatted message. "
            f"Use section names as bold headings, channel names as subheadings, and key points as bullet lists. "
            f"Omit participant lists, message counts, and links. Keep it scannable."
        )
    else:
        dm_line = "6. (Skip channel post)"

    template = (BASE / "digest_prompt.txt").read_text()
    # Strip comment lines before substitution
    lines = [l for l in template.splitlines() if not l.startswith("#")]
    return "\n".join(lines).format(
        window_start=window_start,
        window_end=window_end,
        run_id=run_id,
        output_path=output_path,
        sections_block=sections_block,
        dm_line=dm_line,
    )


# Tools the digest subprocess needs: Slack MCP reads/sends + file Write.
# Names match the server key in ~/.claude/settings.json (adjust if yours differs).
_DIGEST_ALLOWED_TOOLS = (
    "mcp__slack__slack_read_channel,"
    "mcp__slack__slack_read_thread,"
    "mcp__slack__slack_send_message,"
    "mcp__slack__slack_search_users,"
    "Write,Read"
)


@app.route("/digest/start", methods=["POST"])
def digest_start():
    global _digest_proc

    status_path = DATA / "digest.status"
    with _digest_lock:
        if status_path.exists() and status_path.read_text().strip() == "running":
            if _digest_proc and _digest_proc.poll() is None:
                return {"error": "Digest already running."}, 409

    data = request.get_json(force=True) or {}
    now = datetime.now(timezone.utc)
    window_start_dt = _parse_dt(data.get("window_start"), now - timedelta(hours=24))
    window_end_dt = _parse_dt(data.get("window_end"), window_start_dt + timedelta(hours=24))
    if window_end_dt <= window_start_dt:
        return {"error": "window_end must be after window_start."}, 400
    window_start = window_start_dt.isoformat()
    window_end = window_end_dt.isoformat()
    run_id = (
        datetime.fromisoformat(window_start).strftime("%Y-%m-%dT%H-%M")
        + "--"
        + datetime.fromisoformat(window_end).strftime("%Y-%m-%dT%H-%M")
    )

    config_name = data.get("config_name") or request.cookies.get("digest_config", "")
    if not config_name:
        return {"error": "config_name is required."}, 400

    cfg.digests_dir(config_name).mkdir(parents=True, exist_ok=True)
    output_path = str(cfg.digests_dir(config_name) / f"{run_id}.json")

    log_path = DATA / "digest.log"
    log_path.write_text("")
    status_path.write_text("running")
    post_to_slack = bool(data.get("post_to_slack", False))
    prompt = _build_digest_prompt(window_start, window_end, run_id, output_path, config_name=config_name, post_to_slack=post_to_slack)

    started_at = time.time()
    try:
        with _digest_lock:
            _digest_proc = subprocess.Popen(
                ["claude", "-p", prompt,
                 "--allowedTools", _DIGEST_ALLOWED_TOOLS,
                 "--output-format", "stream-json",
                 "--verbose"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(BASE),
            )
            proc = _digest_proc  # capture by value so _reader is unaffected by future assignments
    except OSError as e:
        status_path.write_text("error")
        return {"error": f"Failed to start digest: {e}"}, 500

    def _reader():
        cost_usd: float | None = None
        with open(log_path, "a") as f:
            for raw in proc.stdout:
                raw = raw.rstrip("\n")
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                    etype = ev.get("type", "")
                    if etype == "assistant":
                        for block in ev.get("message", {}).get("content", []):
                            if block.get("type") == "text":
                                text = block["text"].strip()
                                if text:
                                    f.write(text + "\n")
                                    f.flush()
                    elif etype == "tool_use":
                        f.write(f"[{ev.get('name', 'tool')}]\n")
                        f.flush()
                    elif etype == "result":
                        cost_usd = ev.get("total_cost_usd")
                except (json.JSONDecodeError, KeyError, ValueError):
                    f.write(raw + "\n")
                    f.flush()
        ret = proc.wait()
        duration_s = round(time.time() - started_at, 1)
        if ret == 0:
            digest_file = cfg.digests_dir(config_name) / f"{run_id}.json"
            try:
                raw = digest_file.read_text()
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    from json_repair import repair_json
                    d = json.loads(repair_json(raw))
                    digest_file.write_text(json.dumps(d, indent=2))
                d["duration_s"] = duration_s
                if cost_usd is not None:
                    d["cost_usd"] = cost_usd
                digest_file.write_text(json.dumps(d, indent=2))
            except Exception as e:
                print(f"Warning: could not write duration/cost to {digest_file}: {e}", file=sys.stderr)
        status_path.write_text("done" if ret == 0 else "error")

    Thread(target=_reader, daemon=True).start()
    return {"status": "started", "run_id": run_id}, 202


@app.route("/digest/cancel", methods=["POST"])
def digest_cancel():
    global _digest_proc
    if _digest_proc and _digest_proc.poll() is None:
        _digest_proc.terminate()
        try:
            _digest_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _digest_proc.kill()
        (DATA / "digest.status").write_text("error")
    return "", 204


@app.route("/digest/stream")
def digest_stream():
    log_path = DATA / "digest.log"
    status_path = DATA / "digest.status"

    def generate():
        try:
            with open(log_path, "r") as lf:
                while True:
                    for line in lf.read().splitlines():
                        if line:
                            yield f"data: {json.dumps(line)}\n\n"
                    status = status_path.read_text().strip() if status_path.exists() else "idle"
                    if status in ("done", "error", "idle"):
                        for line in lf.read().splitlines():
                            if line:
                                yield f"data: {json.dumps(line)}\n\n"
                        yield f"event: status\ndata: {status}\n\n"
                        return
                    time.sleep(0.3)
        except (FileNotFoundError, GeneratorExit):
            pass

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── post-scrape: diff + Claude analysis ───────────────────────────────────────

def _load_ai_diff(config_name: str) -> dict | None:
    path = cfg.config_dir(config_name) / "ai-diff.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _post_scrape(config_name: str) -> None:
    """Run after a successful scrape: compute diff, call Claude, write ai-diff.json."""
    import diff as df

    prev_path = DATA / "sidebar.prev.json"
    new_path = DATA / "sidebar.json"

    if not prev_path.exists() or not new_path.exists():
        return

    try:
        old_sidebar = json.loads(prev_path.read_text())
        new_sidebar = json.loads(new_path.read_text())
    except json.JSONDecodeError:
        return

    old_config_sections: list[dict] = []
    if cfg.config_exists(config_name):
        prev_config_path = cfg.config_dir(config_name) / "config.prev.json"
        if prev_config_path.exists():
            try:
                old_config_sections = json.loads(prev_config_path.read_text()).get("sections", [])
            except json.JSONDecodeError:
                pass

    structural = df.compute_diff(old_sidebar, new_sidebar)
    ai = df.call_claude(old_sidebar, new_sidebar, structural, old_config_sections)

    combined = {**structural, **ai}
    if cfg.config_exists(config_name):
        (cfg.config_dir(config_name) / "ai-diff.json").write_text(json.dumps(combined, indent=2))


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", default=None)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    url = f"http://localhost:{args.port}"
    if args.run:
        url += f"/digests/{args.run}"
    t = Timer(1.0, lambda: webbrowser.open(url))
    t.daemon = True
    t.start()
    app.run(port=args.port, threaded=True)
