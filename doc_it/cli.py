"""
cli.py — CLI entrypoint

`doc-it run` is the single command. Supports an optional --repo flag
so it can be pointed at any git repo instead of running from inside one.

Two modes detected automatically from .doc-it-state.json presence:
  init   — first run, no state file: summarizes full commit history, creates DEVLOG.md
  update — subsequent runs: reads new commits via LangGraph, appends session entry
"""

import time
import threading
import webbrowser
import http.server
import socketserver
import os
from collections import defaultdict
from pathlib import Path

import click

from doc_it.git_reader import get_repo_root, get_commits_since, get_diff_for_commit, get_files_changed
from doc_it.state import read_state, write_state, ensure_gitignore
from doc_it.chains import load_env, make_llm, summarize_commit, summarize_project, summarize_session, detect_pointers
from doc_it.renderer import (
    get_devlog_path,
    render_init_entries,
    create_devlog,
    update_project_overview,
    read_previous_entries,
    write_devlog_json,
)
from doc_it.graph import run_update_graph
from doc_it.graph_renderer import render_graph
from doc_it.config import read_noir_config, write_noir_config, get_config_path
from doc_it.noir import make_noir_llm

# Gemma free tier: 15K tokens per minute.
# Pause between LLM calls in init mode to avoid RESOURCE_EXHAUSTED.
_INTER_CALL_DELAY = 15  # seconds


def _resolve_llm(mode: str):
    """
    Returns the correct LLM instance based on --mode flag.
    Centralizes the gemini/noir switch so it isn't duplicated across init and update paths.
    """
    if mode == "noir":
        cfg = read_noir_config()
        if cfg is None:
            raise click.ClickException(
                "Noir mode is not configured. Run `doc-it noir setup` first."
            )
        try:
            return make_noir_llm(cfg)
        except ValueError as e:
            raise click.ClickException(str(e))
    return make_llm()


@click.group()
def cli():
    """doc-it: auto-generate human-readable dev logs from git history."""
    pass


@cli.command()
@click.option(
    "--mode",
    default="gemini",
    type=click.Choice(["gemini", "noir"]),
    show_default=True,
    help="LLM backend. 'gemini' uses Google Gemini API; 'noir' uses a local OpenAI-compatible server.",
)
@click.option(
    "--repo",
    default=None,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Path to the target git repo. Defaults to the current directory.",
)
def run(repo: Path | None, mode: str):
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

    # Gemma rate-limit delay only applies to gemini mode; local servers have no limits.
    inter_call_delay = 0 if mode == "noir" else _INTER_CALL_DELAY

    if mode == "gemini":
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
        llm = _resolve_llm(mode)

        summaries = []
        for i, c in enumerate(commits):
            click.echo(f"  summarizing [{c['short_sha']}] {c['message']}...")
            diff = get_diff_for_commit(repo_root, c["sha"])
            summaries.append(summarize_commit(c, diff, llm))
            if i < len(commits) - 1:
                time.sleep(inter_call_delay)

        click.echo("  generating project overview...")
        time.sleep(inter_call_delay)
        project_summary = summarize_project(list(reversed(summaries)), llm)

        # Build per-session summaries grouped by date (oldest first)
        _date_summaries: dict = defaultdict(list)
        for c, s in zip(reversed(list(commits)), reversed(list(summaries))):
            _date_summaries[c["date"][:10]].append(s)

        click.echo("  generating session summaries...")
        session_summary_map: dict = {}
        for date, sess_sums in sorted(_date_summaries.items()):
            time.sleep(inter_call_delay)
            session_summary_map[date] = summarize_session(sess_sums, llm)

        init_entries = render_init_entries(commits, summaries, session_summary_map)

        try:
            create_devlog(repo_root, init_entries, project_summary)
        except Exception as e:
            raise click.ClickException(f"Failed to write DEVLOG.md: {e}")

        # Build session structure for JSON manifest (init mode: group by date)
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

        llm = _resolve_llm(mode)

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


@cli.command()
@click.option(
    "--repo",
    default=None,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Path to the target git repo. Defaults to the current directory.",
)
@click.option("--port", default=4242, show_default=True, help="Port to serve on.")
def serve(repo: Path | None, port: int):
    """
    Serve graph.html over http://localhost so Chrome renders it correctly.

    Regenerates graph.html first, then opens the browser automatically.
    Press Ctrl+C to stop.
    """
    try:
        repo_root = get_repo_root(path=repo)
    except RuntimeError as e:
        raise click.ClickException(str(e))

    # Regenerate graph.html before serving
    try:
        out_path = render_graph(repo_root)
    except (FileNotFoundError, RuntimeError) as e:
        raise click.ClickException(str(e))

    click.echo(f"+ Graph written: {out_path}")

    # Serve from the repo root directory
    os.chdir(repo_root)

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # suppress per-request log noise

    url = f"http://localhost:{port}/graph.html"

    # Open browser after a short delay so the server is ready
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    click.echo(f"+ Serving at {url}")
    click.echo("  Press Ctrl+C to stop.\n")

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", port), QuietHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            httpd.shutdown()
            click.echo("\nStopped.")


@cli.command()
@click.option(
    "--repo",
    default=None,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Path to the target git repo. Defaults to the current directory.",
)
def graph(repo: Path | None):
    """
    Generate an interactive commit graph from devlog.json.

    Writes graph.html to the repo root — open it in any browser.
    No server required.
    """
    try:
        repo_root = get_repo_root(path=repo)
    except RuntimeError as e:
        raise click.ClickException(str(e))

    click.echo(f"Repo: {repo_root}")

    try:
        out_path = render_graph(repo_root)
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    except RuntimeError as e:
        raise click.ClickException(str(e))

    click.echo(f"+ Graph written: {out_path}")
    click.echo("  Open graph.html in your browser to explore the commit graph.")


@cli.group()
def noir():
    """Noir mode: use a local LLM server instead of Google Gemini."""
    pass


@noir.command()
def setup():
    """
    Interactive wizard to configure noir mode.
    Saves settings to ~/.doc-it/config.json.
    """
    import questionary

    click.echo("doc-it noir setup\n")

    backend = questionary.select(
        "Select backend:",
        choices=[
            "Local LLM server (LM Studio, Ollama, etc.)",
            "Local NLP — coming soon",
        ],
    ).ask()

    if backend is None:
        click.echo("Setup cancelled.")
        return

    if backend == "Local NLP — coming soon":
        click.echo(
            "\nLocal NLP mode is coming in a future release.\n"
            "It will run inference fully offline using a lightweight NLP model.\n"
            "Use 'Local LLM server' for now with LM Studio or Ollama."
        )
        return

    url = questionary.text(
        "Local LLM server URL:",
        default="http://localhost:1234/v1",
    ).ask()
    if url is None:
        click.echo("Setup cancelled.")
        return

    model = questionary.text(
        "Model name (must match what your server is running):",
        default="qwen2.5-7b",
    ).ask()
    if model is None:
        click.echo("Setup cancelled.")
        return

    click.echo(f"\nConfiguration:\n  URL:   {url}\n  Model: {model}\n  Temp:  0.2\n")

    confirmed = questionary.confirm("Save this configuration?", default=True).ask()
    if not confirmed:
        click.echo("Setup cancelled. Nothing was saved.")
        return

    write_noir_config(url, model, 0.2)
    click.echo(f"\n+ Config saved: {get_config_path()}")
    click.echo("  Run `doc-it run --mode noir` to use your local model.")


def main():
    cli()
