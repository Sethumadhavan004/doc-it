"""
graph.py — LangGraph StateGraph for the update pipeline

Orchestrates the update mode pipeline as a stateful directed graph.
Each node is an isolated function that reads from and writes to a shared
TypedDict state. LangGraph drives execution order via edges.

Pipeline:
  load_commits → [conditional] → load_previous_entries → summarize
               ↓ (no commits)     → detect_pointers → render → write
              END

The conditional edge after load_commits implements the bail-out path:
if no new commits exist, the graph exits cleanly without calling the LLM.
This makes the graph self-contained — callers don't need to pre-validate.
"""

from typing import TypedDict
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, START, END

from doc_it.git_reader import get_commits_since, get_diff_for_commit
from doc_it.chains import summarize_commit, detect_pointers
from doc_it.renderer import render_session_entry, append_to_devlog, read_previous_entries
from doc_it.state import write_state


class UpdateState(TypedDict):
    repo_root:        object   # Path
    llm:              object   # ChatGoogleGenerativeAI
    last_commit_sha:  str
    commits:          list
    summaries:        list
    pointers:         list
    entry:            str
    previous_entries: list


# ---------------------------------------------------------------------------
# Nodes — each reads state, returns a partial dict of only the keys it updates
# ---------------------------------------------------------------------------

def load_commits_node(state: UpdateState) -> dict:
    print(f"  [node] load_commits — fetching since {state['last_commit_sha'][:7]}")
    commits = get_commits_since(state["repo_root"], sha=state["last_commit_sha"])
    print(f"  [node] load_commits — found {len(commits)} commit(s)")
    return {"commits": commits}


def load_previous_entries_node(state: UpdateState) -> dict:
    print("  [node] load_previous_entries — reading DEVLOG.md")
    entries = read_previous_entries(state["repo_root"])
    print(f"  [node] load_previous_entries — found {len(entries)} previous commit(s)")
    return {"previous_entries": entries}


def summarize_node(state: UpdateState) -> dict:
    print(f"  [node] summarize — processing {len(state['commits'])} commit(s)")
    summaries = []
    for c in state["commits"]:
        print(f"    summarizing [{c['short_sha']}] {c['message']}...")
        diff = get_diff_for_commit(state["repo_root"], c["sha"])
        summaries.append(summarize_commit(c, diff, state["llm"]))
    return {"summaries": summaries}


def detect_pointers_node(state: UpdateState) -> dict:
    print(f"  [node] detect_pointers — checking {len(state['commits'])} commit(s)")
    pointers = []
    for c, summary in zip(state["commits"], state["summaries"]):
        related = detect_pointers(c, summary, state["previous_entries"], state["llm"])
        pointers.append(related)
        if related:
            print(f"    [{c['short_sha']}] -> related: {[r['short_sha'] for r in related]}")
        else:
            print(f"    [{c['short_sha']}] -> no related commits")
    return {"pointers": pointers}


def render_node(state: UpdateState) -> dict:
    print("  [node] render — assembling markdown entry")
    entry = render_session_entry(state["commits"], state["summaries"], state["pointers"])
    return {"entry": entry}


def write_node(state: UpdateState) -> dict:
    """
    Writes DEVLOG.md and saves state last — if any earlier node fails,
    state is not updated and the next run retries from the same point.
    """
    print("  [node] write — appending to DEVLOG.md")
    append_to_devlog(state["repo_root"], state["entry"])
    latest_sha = state["commits"][0]["sha"]
    write_state(state["repo_root"], latest_sha)
    print(f"  [node] write — state saved. Last commit: {latest_sha[:7]}")
    return {}


# ---------------------------------------------------------------------------
# Router — decides next hop after load_commits
# ---------------------------------------------------------------------------

def route_after_load(state: UpdateState) -> str:
    if len(state["commits"]) == 0:
        print("  [router] no new commits — bailing early")
        return END
    print(f"  [router] {len(state['commits'])} commit(s) found — continuing")
    return "load_previous_entries"


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

def build_update_graph():
    graph = StateGraph(UpdateState)

    graph.add_node("load_commits",          load_commits_node)
    graph.add_node("load_previous_entries", load_previous_entries_node)
    graph.add_node("summarize",             summarize_node)
    graph.add_node("detect_pointers",       detect_pointers_node)
    graph.add_node("render",                render_node)
    graph.add_node("write",                 write_node)

    graph.add_edge(START, "load_commits")
    graph.add_conditional_edges("load_commits", route_after_load)
    graph.add_edge("load_previous_entries", "summarize")
    graph.add_edge("summarize",             "detect_pointers")
    graph.add_edge("detect_pointers",       "render")
    graph.add_edge("render",                "write")
    graph.add_edge("write",                 END)

    return graph.compile()


def run_update_graph(repo_root, llm, last_commit_sha: str) -> dict:
    """
    Runs the update pipeline. Returns the final state dict.
    cli.py reads state["commits"] to report what was processed.
    """
    compiled = build_update_graph()
    return compiled.invoke({
        "repo_root":        repo_root,
        "llm":              llm,
        "last_commit_sha":  last_commit_sha,
        "commits":          [],
        "summaries":        [],
        "pointers":         [],
        "entry":            "",
        "previous_entries": [],
    })
