"""
config.py — global user configuration for doc-it modes.
Location: ~/.doc-it/config.json

Schema:
{
  "noir": {
    "backend": "local_llm",
    "local_llm_url": "http://localhost:1234/v1",
    "local_llm_model": "qwen2.5-7b",
    "temperature": 0.2
  }
}
"""

import json
from pathlib import Path

CONFIG_DIR  = Path.home() / ".doc-it"
CONFIG_FILE = CONFIG_DIR / "config.json"


def get_config_path() -> Path:
    return CONFIG_FILE


def read_noir_config() -> dict | None:
    """
    Returns the noir config dict, or None if not configured yet.
    Treats a missing file, missing 'noir' key, or corrupted JSON as unconfigured.
    """
    if not CONFIG_FILE.exists():
        return None
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data.get("noir") or None
    except (json.JSONDecodeError, OSError):
        return None


def write_noir_config(url: str, model: str, temperature: float) -> None:
    """
    Persists noir config. Merges into the full config dict so any other
    top-level keys added in the future are preserved.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if CONFIG_FILE.exists():
        try:
            existing = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    existing["noir"] = {
        "backend":         "local_llm",
        "local_llm_url":   url,
        "local_llm_model": model,
        "temperature":     temperature,
    }

    CONFIG_FILE.write_text(json.dumps(existing, indent=2), encoding="utf-8")
