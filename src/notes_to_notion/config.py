"""Load config.toml and merge it over sensible defaults."""
import copy
import os
import tomllib
from zoneinfo import ZoneInfo

DEFAULT_CONFIG_PATH = "~/.config/notes-to-notion/config.toml"

ITEM_TYPES = ["Task", "Event", "Reference", "Idea", "Contact", "Shopping", "Question"]
PRIORITIES = ["High", "Medium", "Low"]
EFFORTS = ["Small", "Medium", "Large"]
CONFIDENCE = ["High", "Medium", "Low"]
STATUSES = ["Proposed", "Approved", "Filed", "Rejected"]

DEFAULTS = {
    "notes": {
        "account": "iCloud",
        "days": 30,
        "skip_folders": ["Recently Deleted"],
        "state_dir": "~/.local/state/notes-to-notion",
    },
    "ai": {
        "provider": "anthropic",  # anthropic | openai | ollama
        "model": "",
        "base_url": "",
        "api_key_env": "",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "notion": {
        "api_key_env": "NOTION_API_KEY",
        "inbound_data_source_id": "",
        "tracker_data_source_id": "",
        "assignee_user_id": "",
    },
    "taxonomy": {
        "timezone": "America/Los_Angeles",
        "categories": ["Work", "School", "Personal", "Errands", "Side Project"],
        "default_category": "Personal",
        "category_hints": {},
        "task_types": ["Study", "Software", "Content", "Admin", "Other"],
    },
    "filters": {
        "skip_keywords": [],
        "skip_description": "",
    },
    "translator": {
        "expansions": {},
        "extra_rules": "",
    },
    "tracker": {
        "title": "Task name",
        "category": "Category",
        "priority": "Priority",
        "effort": "Effort level",
        "task_type": "Task type",
        "due_date": "Due date",
        "target_finish": "Target finish",
        "description": "Description",
        "status": "Status",
        "status_type": "status",  # "status" or "select", matching the tracker property type
        "status_value": "Not started",
        "assignee": "Assignee",
    },
    "calendar": {
        "provider": "none",  # none | google
        "google_token_file": "",
        "calendar_id": "primary",
        "event_minutes": 60,
    },
}


class ConfigError(ValueError):
    pass


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | None = None, require_file: bool = True) -> dict:
    path = os.path.expanduser(path or os.environ.get("N2N_CONFIG") or DEFAULT_CONFIG_PATH)
    user = {}
    if os.path.exists(path):
        with open(path, "rb") as f:
            user = tomllib.load(f)
    elif require_file:
        raise ConfigError(f"No config at {path}. Run `n2n init` to create one.")
    cfg = _merge(DEFAULTS, user)
    cfg["_path"] = path
    validate(cfg)
    return cfg


def validate(cfg: dict) -> None:
    tax = cfg["taxonomy"]
    for name in tax["categories"] + tax["task_types"]:
        if "," in name:
            raise ConfigError(f"Notion select options can't contain commas: {name!r}")
    if tax["default_category"] not in tax["categories"]:
        raise ConfigError("taxonomy.default_category must be one of taxonomy.categories")
    try:
        ZoneInfo(tax["timezone"])
    except Exception:
        raise ConfigError(f"Unknown timezone {tax['timezone']!r} (use an IANA name like 'Pacific/Honolulu')")
    if cfg["ai"]["provider"] not in ("anthropic", "openai", "ollama"):
        raise ConfigError("ai.provider must be anthropic, openai, or ollama")
    if int(cfg["notes"]["days"]) > 30:
        raise ConfigError("notes.days can't exceed 30 (hard privacy limit)")


def secret(env_name: str) -> str:
    """Read a secret from the environment. Never from the config file."""
    val = os.environ.get(env_name, "").strip()
    if not val:
        raise ConfigError(f"Set the {env_name} environment variable.")
    return val


def state_dir(cfg: dict) -> str:
    d = os.path.expanduser(cfg["notes"]["state_dir"])
    os.makedirs(d, exist_ok=True)
    return d
