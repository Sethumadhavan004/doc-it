"""
graph_renderer.py — HTML graph generator

Reads devlog.json and produces a self-contained graph.html file at the
repo root. The HTML file embeds the full devlog data as a JS constant and
renders an interactive D3 force-directed commit graph.

No server required — open graph.html directly in any browser.

Backfill logic
--------------
devlog.json is only written when `doc-it run` executes. If the tool was
added mid-project, earlier commits exist in git but are absent from
devlog.json. `backfill_devlog_json()` detects this gap by comparing the
SHAs in devlog.json against the full git log, then rebuilds devlog.json
from scratch covering all commits. Summaries and pointers for backfilled
commits are left empty — they were never processed by the LLM.
`render_graph()` calls this automatically before rendering.
"""

import json
from collections import defaultdict
from pathlib import Path

from doc_it.git_reader import get_commits_since, get_files_changed
from doc_it.renderer import write_devlog_json

TEMPLATE_PATH       = Path(__file__).parent / "templates" / "graph.html"
D3_PATH             = Path(__file__).parent / "templates" / "d3.min.js"
GRAPH_HTML_FILENAME = "graph.html"


def backfill_devlog_json(repo_root: Path) -> None:
    """
    Ensures devlog.json contains every commit in the git history.

    How it works:
    1. Read all commits from git (full history, oldest → newest)
    2. Read existing devlog.json to get already-processed commit data
       (summaries, pointers, tags) keyed by short_sha
    3. For any commit not in devlog.json, create a stub entry with
       empty summary and pointers — structural data only
    4. Rewrite devlog.json with the complete set, grouped by date

    This is safe to call repeatedly — it only adds missing commits,
    preserving all existing LLM-generated summaries and pointers.
    """
    json_path = repo_root / "devlog.json"

    # Load existing processed data keyed by short_sha
    existing_by_sha: dict = {}
    project_summary = ""
    if json_path.exists():
        try:
            existing = json.loads(json_path.read_text(encoding="utf-8"))
            project_summary = existing.get("project_summary", "")
            for session in existing.get("sessions", []):
                for c in session.get("commits", []):
                    existing_by_sha[c["short_sha"]] = c
        except (json.JSONDecodeError, OSError):
            pass

    # Get full git history (sha=None → all commits, returns newest-first)
    all_git_commits = get_commits_since(repo_root, sha=None)
    if not all_git_commits:
        return

    # Check if backfill is needed
    git_shas = {c["short_sha"] for c in all_git_commits}
    known_shas = set(existing_by_sha.keys())
    if git_shas == known_shas:
        return  # already complete, nothing to do

    # Rebuild: merge git history with existing processed data
    # Reverse to chronological order for grouping
    all_git_commits.reverse()

    date_map: dict = defaultdict(list)
    for c in all_git_commits:
        short_sha = c["short_sha"]
        if short_sha in existing_by_sha:
            # Preserve LLM-generated fields from existing entry
            entry = existing_by_sha[short_sha]
        else:
            # Stub entry — structural data only, no LLM content
            entry = {
                "sha":           c["sha"],
                "short_sha":     short_sha,
                "message":       c["message"],
                "author":        c["author"],
                "date":          c["date"],
                "summary":       "",
                "tags":          _extract_tags(c["message"]),
                "files_changed": get_files_changed(repo_root, c["sha"]),
                "pointers":      [],
            }
        date_map[c["date"][:10]].append(entry)

    sessions = [
        {"date": date, "commits": date_map[date]}
        for date in sorted(date_map.keys())
    ]
    write_devlog_json(repo_root, sessions, project_summary)


def _extract_tags(message: str) -> list[str]:
    """Duplicate of renderer._extract_tags — avoids a private import."""
    import re
    match = re.match(r"^([a-zA-Z]+)[\s:(/]", message.strip())
    return [match.group(1).lower()] if match else []


def render_graph(repo_root: Path) -> Path:
    """
    Backfills devlog.json if needed, then renders graph.html.

    Returns the path to the generated graph.html.
    Raises FileNotFoundError if devlog.json does not exist after backfill attempt.
    Raises RuntimeError if the template is missing.
    """
    json_path = repo_root / "devlog.json"
    if not json_path.exists():
        raise FileNotFoundError(
            "devlog.json not found. Run `doc-it run` first to generate it."
        )

    # Fill any historical gaps before rendering
    backfill_devlog_json(repo_root)

    if not TEMPLATE_PATH.exists():
        raise RuntimeError(
            f"Graph template missing: {TEMPLATE_PATH}\n"
            "This is a doc-it installation issue — reinstall the package."
        )

    devlog = json.loads(json_path.read_text(encoding="utf-8"))

    # Compute summary stats for the header
    total_commits = sum(s["commit_count"] for s in devlog.get("sessions", []))
    total_sessions = len(devlog.get("sessions", []))
    repo_name = devlog.get("repo", repo_root.name)

    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    # Inline D3 so graph.html works from file:// with no CDN dependency.
    d3_inline = ""
    if D3_PATH.exists():
        d3_source = D3_PATH.read_text(encoding="utf-8")
        d3_inline = f"<script>{d3_source}</script>"

    # Simple string substitution — no Jinja dependency needed at this stage.
    # The devlog JSON is embedded directly as a JS literal.
    html = (
        template
        .replace("{{repo}}",           repo_name)
        .replace("{{total_commits}}",  str(total_commits))
        .replace("{{total_sessions}}", str(total_sessions))
        .replace("{{devlog_json}}",    json.dumps(devlog, ensure_ascii=False))
        .replace(
            '<script src="https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"></script>',
            d3_inline,
        )
    )

    out_path = repo_root / GRAPH_HTML_FILENAME
    out_path.write_text(html, encoding="utf-8")
    return out_path