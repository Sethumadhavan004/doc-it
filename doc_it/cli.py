"""
cli.py — CLI entrypoint

`doc-it run` is the single command. Supports an optional --repo flag
so it can be pointed at any git repo instead of running from inside one.

Two modes detected automatically from .doc-it-state.json presence:
  init   — first run, no state file: summarizes full commit history, creates DEVLOG.md
  update — subsequent runs: reads new commits via LangGraph, appends session entry
"""

import time
from pathlib import Path

import click

from doc_it.git_reader import get_repo_root, get_commits_since, get_diff_for_commit, get_files_changed
from doc_it.state import read_state, write_state, ensure_gitignore
from doc_it.chains import load_env, make_llm, summarize_commit, summarize_project, detect_pointers
from doc_it.renderer import (
    get_devlog_path,
    render_init_entries,
    create_devlog,
    read_previous_entries,
    write_devlog_json,
)
from doc_it.graph import run_update_graph

# Gemma free tier: 15K tokens per minute.
# Pause between LLM calls in init mode to avoid RESOURCE_EXHAUSTED.
_INTER_CALL_DELAY = 15  # seconds


@click.group()
def cli():
    """doc-it: auto-generate human-readable dev logs from git history."""
    pass


@cli.command()
@click.option(
    "--repo",
    default=None,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Path to the target git repo. Defaults to the current directory.",
)
def run(repo: Path | None):
    """
    Generate a DEVLOG entry for the current session.

    First run  → init mode: summarizes full commit history, creates DEVLOG.md.
    Subsequent → update mode: reads new commits since last run, appends entry.
    """
    # Resolve repo root — from --repo flag or cwd
    try:
        repo_root = get_repo_root(path=repo)
    except RuntimeError as e:
        raise click.ClickException(str(e))

    click.echo(f"Repo: {repo_root}")

    try:
        load_env()
    except EnvironmentError as e:
        raise click.ClickException(str(e))

    state = read_state(repo_root)

    if state is None:
        # -------------------------------------------------------------------
        # INIT MODE
        # -------------------------------------------------------------------
        click.echo("Mode: INIT (first run — documenting full commit history)")

        ensure_gitignore(repo_root)
        click.echo("+ Added .doc-it-state.json to .gitignore")

        commits = get_commits_since(repo_root, sha=None)
        if not commits:
            raise click.ClickException(
                "No commits found. Make at least one commit before running doc-it."
            )

        click.echo(f"+ Found {len(commits)} commit(s) — summarizing...")
        llm = make_llm()

        summaries = []
        for i, c in enumerate(commits):
            click.echo(f"  summarizing [{c['short_sha']}] {c['message']}...")
            diff = get_diff_for_commit(repo_root, c["sha"])
            summaries.append(summarize_commit(c, diff, llm))
            if i < len(commits) - 1:
                time.sleep(_INTER_CALL_DELAY)

        click.echo("  generating project overview...")
        time.sleep(_INTER_CALL_DELAY)
        project_summary = summarize_project(list(reversed(summaries)), llm)

        init_entries = render_init_entries(commits, summaries)

        try:
            create_devlog(repo_root, init_entries, project_summary)
        except Exception as e:
            raise click.ClickException(f"Failed to write DEVLOG.md: {e}")

        # Build session structure for JSON manifest (init mode: group by date)
        from collections import defaultdict
        date_map: dict = defaultdict(list)
        for c, summary in zip(reversed(commits), reversed(summaries)):
            commit_date = c["date"][:10]
            date_map[commit_date].append({
                **c,
                "summary":       summary,
                "files_changed": get_files_changed(repo_root, c["sha"]),
                "pointers":      [],
            })
        sessions = [
            {"date": date, "commits": date_map[date]}
            for date in sorted(date_map.keys())
        ]
        write_devlog_json(repo_root, sessions, project_summary)

        write_state(repo_root, commits[0]["sha"])

        click.echo(f"\n+ DEVLOG created: {get_devlog_path(repo_root)}")
        click.echo(f"+ State saved. Last commit: {commits[0]['short_sha']}")
        click.echo("\n[doc-it init complete — run again after your next commit]")

    else:
        # -------------------------------------------------------------------
        # UPDATE MODE
        # -------------------------------------------------------------------
        last_commit = state["last_commit"]
        last_run    = state["last_run"]
        click.echo(f"Mode: UPDATE (last run: {last_run}, since: {last_commit[:7]})")

        llm = make_llm()

        click.echo("Running update graph...")
        try:
            final_state = run_update_graph(repo_root, llm, last_commit)
        except Exception as e:
            raise click.ClickException(f"Graph run failed: {e}")

        commits_processed = final_state.get("commits", [])

        if not commits_processed:
            click.echo("No new commits since last run. Nothing to document.")
            return

        click.echo(f"\n+ Processed {len(commits_processed)} commit(s)")
        click.echo(f"+ DEVLOG written: {get_devlog_path(repo_root)}")
        click.echo(f"+ State updated. Last commit: {commits_processed[0]['short_sha']}")


def main():
    cli()
