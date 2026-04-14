"""
renderer.py — markdown assembly and DEVLOG.md writer

Pure string assembly and file I/O. No LangChain.

Responsibilities:
  - make_anchor()           — compute GitHub-compatible heading anchors for pointer links
  - read_previous_entries() — parse existing DEVLOG headings for pointer detection input
  - render_session_entry()  — assemble a single update-mode session block
  - render_init_entries()   — assemble full history grouped by date (init mode)
  - create_devlog()         — write DEVLOG.md from scratch with project overview header
  - append_to_devlog()      — append an update session entry to existing DEVLOG.md
"""

import re
import json
from pathlib import Path
from datetime import datetime

DEVLOG_FILENAME = "DEVLOG.md"

# Matches commit headings written by this renderer: ### [abc1234] message
COMMIT_HEADING_RE = re.compile(r"^### \[([a-f0-9]{7})\] (.+)$", re.MULTILINE)


def get_devlog_path(repo_root: Path) -> Path:
    return repo_root / DEVLOG_FILENAME


def make_anchor(commit: dict) -> str:
    """
    Computes the GitHub-flavored markdown anchor for a commit heading.

    Heading format: [{short_sha}] {message}
    Anchor rules: lowercase → spaces to hyphens → strip non-word chars → collapse hyphens.

    Used when writing pointer links so the href always matches the heading exactly.
    """
    heading = f"[{commit['short_sha']}] {commit['message']}"
    anchor = heading.lower().replace(" ", "-")
    anchor = re.sub(r"[^\w\-]", "", anchor)
    anchor = re.sub(r"-+", "-", anchor).strip("-")
    return anchor


def read_previous_entries(repo_root: Path) -> list[dict]:
    """
    Parses DEVLOG.md and returns all documented commits as structured entries.
    Each entry: {short_sha, message, anchor}

    DEVLOG.md is the single source of truth — no separate index file.
    Returns empty list if DEVLOG.md doesn't exist yet.
    """
    devlog_path = get_devlog_path(repo_root)
    if not devlog_path.exists():
        return []

    content = devlog_path.read_text(encoding="utf-8")
    entries = []
    for match in COMMIT_HEADING_RE.finditer(content):
        short_sha = match.group(1)
        message   = match.group(2)
        entries.append({
            "short_sha": short_sha,
            "message":   message,
            "anchor":    make_anchor({"short_sha": short_sha, "message": message}),
        })
    return entries


def render_session_entry(
    commits: list[dict],
    summaries: list[str],
    pointers: list | None = None,
    session_date: str | None = None,
) -> str:
    """
    Assembles a markdown session block for update mode.

    Args:
        commits:      commit dicts (sha, short_sha, message, author, date)
        summaries:    plain English summaries, parallel to commits
        pointers:     related-commit lists, parallel to commits (or None)
        session_date: date override for testing

    Returns: markdown string, does NOT write to disk.
    """
    if session_date is None:
        session_date = datetime.now().strftime("%Y-%m-%d")
    if pointers is None:
        pointers = [None] * len(commits)

    lines = [f"## Session — {session_date}", "", f"**Commits in this session:** {len(commits)}", ""]

    for commit, summary, related in zip(commits, summaries, pointers):
        lines.append(f"### [{commit['short_sha']}] {commit['message']}")
        lines.append(f"*{commit['author']} — {commit['date'][:10]}*")
        lines.append("")
        lines.append(f"> {summary}")
        lines.append("")
        if related:
            pointer_links = ", ".join(
                f"[{r['short_sha']} — {r['message']}](#{r['anchor']})" for r in related
            )
            lines.append(f"**Related:** {pointer_links}")
            lines.append("")

    lines += ["---", ""]
    return "\n".join(lines)


def render_init_entries(commits: list[dict], summaries: list[str]) -> str:
    """
    Assembles the full commit history for init mode, grouped by date, chronological order.

    Commits from git log arrive newest-first — reversed here so the DEVLOG
    reads as a narrative from project start to present.
    Each unique date gets its own ## Session block with a --- separator.

    Args:
        commits:   commit dicts, newest first
        summaries: summaries in the same order as commits

    Returns: markdown string covering all sessions, does NOT write to disk.
    """
    paired = list(zip(commits, summaries))
    paired.reverse()  # oldest first

    lines = []
    current_date = None

    for commit, summary in paired:
        commit_date = commit["date"][:10]

        if commit_date != current_date:
            if current_date is not None:
                lines += ["---", ""]
            lines += [f"## Session — {commit_date}", ""]
            current_date = commit_date
            # Count commits for this date to show the same header as update mode
            date_count = sum(1 for c, _ in paired if c["date"][:10] == commit_date)
            lines += [f"**Commits in this session:** {date_count}", ""]

        lines.append(f"### [{commit['short_sha']}] {commit['message']}")
        lines.append(f"*{commit['author']} — {commit_date}*")
        lines.append("")
        lines.append(f"> {summary}")
        lines.append("")

    lines += ["---", ""]
    return "\n".join(lines)


def create_devlog(repo_root: Path, init_entries: str, project_summary: str = "") -> None:
    """
    Creates DEVLOG.md from scratch with an optional project overview block.
    Called only in init mode.

    Args:
        repo_root:       git repo root
        init_entries:    rendered markdown from render_init_entries()
        project_summary: one-paragraph overview from summarize_project() (optional)
    """
    devlog_path = get_devlog_path(repo_root)

    lines = [
        "# DEVLOG",
        "",
        "Auto-generated by [doc-it](https://github.com/Sethumadhavan004/doc-it).",
        "Each section is one development session.",
        "",
    ]

    if project_summary:
        lines += ["---", "", "## Project Overview", "", project_summary, ""]

    lines += ["---", ""]

    devlog_path.write_text("\n".join(lines) + init_entries, encoding="utf-8")


def append_to_devlog(repo_root: Path, entry: str) -> None:
    """
    Appends a session entry to an existing DEVLOG.md.
    If DEVLOG.md was deleted but state still exists, recreates it.
    """
    devlog_path = get_devlog_path(repo_root)
    if not devlog_path.exists():
        create_devlog(repo_root, entry)
        return
    with devlog_path.open("a", encoding="utf-8") as f:
        f.write(entry)


# ---------------------------------------------------------------------------
# JSON manifest — machine-readable source of truth for the graph renderer
# ---------------------------------------------------------------------------

DEVLOG_JSON_FILENAME = "devlog.json"
DEVLOG_JSON_VERSION  = "1.0"

# Matches conventional commit prefixes: feat, fix, bugfix, test, chore, docs, refactor, etc.
_TAG_RE = re.compile(r"^([a-zA-Z]+)[\s:(/]")


def _extract_tags(message: str) -> list[str]:
    """
    Parses the conventional commit prefix from a commit message.
    'feat: add thing'  → ['feat']
    'bugfix:fix thing' → ['bugfix']
    'random message'   → []
    """
    match = _TAG_RE.match(message.strip())
    if match:
        return [match.group(1).lower()]
    return []


def write_devlog_json(
    repo_root: Path,
    sessions: list[dict],
    project_summary: str = "",
) -> None:
    """
    Writes devlog.json alongside DEVLOG.md — the machine-readable manifest
    consumed by the graph renderer and future LLM ingestion layer.

    Args:
        repo_root:       git repo root
        sessions:        list of session dicts, each containing:
                           {date, commits: [{sha, short_sha, message, author,
                            date, summary, files_changed, pointers}]}
        project_summary: project overview string (may be empty on update runs)

    Schema version: 1.0
    """
    manifest = {
        "version":         DEVLOG_JSON_VERSION,
        "generated_at":    datetime.now().isoformat(timespec="seconds"),
        "repo":            repo_root.name,
        "project_summary": project_summary,
        "sessions":        [],
    }

    for session in sessions:
        session_commits = []
        for c in session["commits"]:
            session_commits.append({
                "sha":           c["sha"],
                "short_sha":     c["short_sha"],
                "message":       c["message"],
                "author":        c["author"],
                "date":          c["date"],
                "summary":       c.get("summary", ""),
                "tags":          _extract_tags(c["message"]),
                "files_changed": c.get("files_changed", []),
                "pointers": [
                    {
                        "short_sha": p["short_sha"],
                        "message":   p["message"],
                        "anchor":    p["anchor"],
                    }
                    for p in c.get("pointers", [])
                ],
            })

        manifest["sessions"].append({
            "session_id":    session["date"],
            "date":          session["date"],
            "commit_count":  len(session_commits),
            "commits":       session_commits,
        })

    json_path = repo_root / DEVLOG_JSON_FILENAME
    json_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
