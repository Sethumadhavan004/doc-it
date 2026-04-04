"""
state.py — run state persistence

Tracks the SHA of the last processed commit so update mode only reads
new commits, not the entire history. Stored as .doc-it-state.json at the
repo root — excluded from git via .gitignore.

Schema: {"last_commit": "<full SHA>", "last_run": "<ISO timestamp>"}
"""

import json
from pathlib import Path
from datetime import datetime

STATE_FILENAME = ".doc-it-state.json"


def get_state_path(repo_root: Path) -> Path:
    return repo_root / STATE_FILENAME


def read_state(repo_root: Path) -> dict | None:
    """
    Returns the state dict, or None if this is the first run.
    None is the signal for init mode.
    Treats a corrupted state file as first run.
    """
    state_path = get_state_path(repo_root)
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_state(repo_root: Path, last_commit_sha: str) -> None:
    """
    Persists state after a successful run.
    Always called AFTER writing DEVLOG.md — if doc-it crashes mid-run,
    state is not updated and the next run retries from the same point.
    """
    state = {
        "last_commit": last_commit_sha,
        "last_run":    datetime.now().isoformat(timespec="seconds"),
    }
    get_state_path(repo_root).write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )


def ensure_gitignore(repo_root: Path) -> None:
    """
    Adds .doc-it-state.json to .gitignore if not already present.
    Called once during init so the state file is never accidentally committed.
    """
    gitignore_path = repo_root / ".gitignore"

    if gitignore_path.exists():
        existing = gitignore_path.read_text(encoding="utf-8")
        if STATE_FILENAME in existing:
            return
        with gitignore_path.open("a", encoding="utf-8") as f:
            f.write(f"\n# doc-it state\n{STATE_FILENAME}\n")
    else:
        gitignore_path.write_text(
            f"# doc-it state\n{STATE_FILENAME}\n",
            encoding="utf-8",
        )
