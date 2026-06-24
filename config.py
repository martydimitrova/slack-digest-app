import json
import sys
from pathlib import Path

BASE = Path(__file__).parent
DATA = BASE / "data"
CONFIGS = DATA / "configs"

def _default_config() -> dict:
    return {"customization_prompt": "", "sections": [], "last_run": None}


def load_sidebar() -> dict:
    p = DATA / "sidebar.json"
    if not p.exists():
        return {"sections": []}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        print(f"Warning: {p} contains invalid JSON", file=sys.stderr)
        return {"sections": []}


def list_configs() -> list[str]:
    if not CONFIGS.exists():
        return []
    return sorted(
        d.name for d in CONFIGS.iterdir()
        if d.is_dir() and (d / "config.json").exists()
    )


def config_exists(name: str) -> bool:
    return bool(name) and (CONFIGS / name).exists()


def config_dir(name: str) -> Path:
    return CONFIGS / name


def digests_dir(name: str) -> Path:
    return DATA / "digests" / name


def load_config(name: str) -> dict:
    p = CONFIGS / name / "config.json"
    if not p.exists():
        return _default_config()
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        print(f"Warning: {p} contains invalid JSON", file=sys.stderr)
        return _default_config()


def save_config(name: str, data: dict) -> None:
    d = CONFIGS / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(data, indent=2))


def create_config(name: str) -> None:
    save_config(name, _default_config())


def merge_new_channels(sidebar: dict, cfg_data: dict) -> list[dict]:
    """Return channels from sidebar not assigned to any section in config."""
    assigned = {
        ch["id"]
        for section in cfg_data.get("sections", [])
        for ch in section.get("channels", [])
    }
    return [
        ch
        for section in sidebar.get("sections", [])
        for ch in section.get("channels", [])
        if ch.get("id") and ch["id"] not in assigned
    ]


