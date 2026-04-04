"""
chains.py — LangChain chains for commit summarization and pointer detection

Three chains:
  make_diff_summary_chain()  — commit message + diff → plain English summary
  make_pointer_chain()       — new commit + past commits → related SHA list
  PROJECT_SUMMARY_PROMPT     — all summaries → one-paragraph project overview

All chains use LCEL (the | operator): prompt | llm | parser.

Note on Gemma compatibility: with_structured_output() is intentionally avoided.
Gemma (gemma-3-4b-it) does not reliably honour JSON schema constraints, causing
silent failures. The pointer chain instead requests a plain comma-separated SHA
list and validates matches against known SHAs on the Python side.
"""

import os
import re
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser


def load_env():
    """
    Loads GOOGLE_API_KEY from the .env file two levels up (phase-2/.env).
    Also accepts GEMINI_API_KEY as an alias for backward compatibility.
    Removes GEMINI_API_KEY after aliasing to suppress LangChain's duplicate-key warning.
    Raises EnvironmentError if neither key is found.
    """
    env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    load_dotenv(dotenv_path=env_path)

    if not os.getenv("GOOGLE_API_KEY") and not os.getenv("GEMINI_API_KEY"):
        raise EnvironmentError(
            "No API key found. Set GOOGLE_API_KEY in your environment or phase-2/.env"
        )
    if not os.getenv("GOOGLE_API_KEY") and os.getenv("GEMINI_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = os.getenv("GEMINI_API_KEY")
    if os.getenv("GOOGLE_API_KEY") and os.getenv("GEMINI_API_KEY"):
        del os.environ["GEMINI_API_KEY"]


def make_llm() -> ChatGoogleGenerativeAI:
    """
    Returns an initialized Gemma LLM instance.
    temperature=0.2 — low randomness for factual, grounded summaries.
    """
    return ChatGoogleGenerativeAI(
        model="gemma-3-4b-it",
        temperature=0.2,
    )


# ---------------------------------------------------------------------------
# Diff summarizer
# ---------------------------------------------------------------------------

DIFF_SUMMARY_PROMPT = ChatPromptTemplate.from_messages([
    ("human", """You are a developer assistant that summarizes git commits.
Given a commit message and its diff, write a 2-3 sentence plain English summary.
Focus on WHAT changed and WHY. Be specific — mention file names or function names if relevant.
Do not use bullet points. Write in past tense. Do not repeat the commit message verbatim.

Commit message: {commit_message}

Diff:
{diff}

Summary:"""),
])


def make_diff_summary_chain(llm: ChatGoogleGenerativeAI):
    return DIFF_SUMMARY_PROMPT | llm | StrOutputParser()


def summarize_commit(commit: dict, diff: str, llm: ChatGoogleGenerativeAI) -> str:
    """
    Summarizes a single commit in plain English.

    Args:
        commit: {sha, short_sha, message, author, date}
        diff:   raw diff string from get_diff_for_commit()
        llm:    initialized LLM instance

    Returns: 2-3 sentence summary string
    """
    return make_diff_summary_chain(llm).invoke({
        "commit_message": commit["message"],
        "diff": diff,
    })


# ---------------------------------------------------------------------------
# Project overview (init mode only)
# ---------------------------------------------------------------------------

PROJECT_SUMMARY_PROMPT = ChatPromptTemplate.from_messages([
    ("human", """You are a developer assistant writing documentation.
Below are summaries of all commits in a software project, in chronological order.
Write a concise 3-4 sentence overview of what this project does, what problem it solves,
and what its key technical components are.
Write in present tense. Be specific — name the key files, patterns, or libraries used.
Do not use bullet points. Do not start with "This project".

Commit summaries (oldest to newest):
{commit_summaries}

Project overview:"""),
])


def summarize_project(commit_summaries: list[str], llm: ChatGoogleGenerativeAI) -> str:
    """
    Produces a one-paragraph project overview from all commit summaries.
    Called once during init — written to the top of DEVLOG.md as permanent context.

    Args:
        commit_summaries: summaries oldest → newest
        llm:              initialized LLM instance

    Returns: 3-4 sentence project overview string
    """
    combined = "\n\n".join(f"[{i+1}] {s}" for i, s in enumerate(commit_summaries))
    chain = PROJECT_SUMMARY_PROMPT | llm | StrOutputParser()
    return chain.invoke({"commit_summaries": combined})


# ---------------------------------------------------------------------------
# Pointer detection
# ---------------------------------------------------------------------------
#
# Asks the LLM for a plain comma-separated SHA list rather than JSON.
# Gemma's structured output support is unreliable — plain text + regex is safer.
# Any SHA the LLM hallcinates that isn't in previous_entries is silently dropped.

class PointerResult(BaseModel):
    """Internal container for parsed pointer detection results."""
    related_shas: list[str] = Field(default_factory=list)


POINTER_PROMPT = ChatPromptTemplate.from_messages([
    ("human", """You are a developer assistant analyzing git commit history.
Given a new commit summary and a list of previous commits, identify which previous
commits are related to the new one.

Two commits are related if they:
- Touch the same file or module
- Work on the same feature or bug
- Are part of the same logical chain of changes

New commit:
  SHA: {new_sha}
  Message: {new_message}
  Summary: {new_summary}

Previous commits (SHA - message):
{previous_commits}

Reply with ONLY one of:
- The word NONE if no previous commits are related
- A comma-separated list of the related short SHAs (7 chars each), e.g.: abc1234, def5678

No explanation. No markdown. Just NONE or the SHA list."""),
])

_SHA_RE = re.compile(r"\b[a-f0-9]{7}\b")


def make_pointer_chain(llm: ChatGoogleGenerativeAI):
    return POINTER_PROMPT | llm | StrOutputParser()


def detect_pointers(
    commit: dict,
    summary: str,
    previous_entries: list[dict],
    llm: ChatGoogleGenerativeAI,
) -> list[dict]:
    """
    Returns previous DEVLOG commits related to the current commit.

    Args:
        commit:           current commit dict
        summary:          plain English summary of the current commit
        previous_entries: from renderer.read_previous_entries() — {short_sha, message, anchor}
        llm:              initialized LLM instance

    Returns: list of related entry dicts (empty if none found)
    """
    if not previous_entries:
        return []

    formatted = "\n".join(
        f"  {e['short_sha']} - {e['message']}" for e in previous_entries
    )

    try:
        response: str = make_pointer_chain(llm).invoke({
            "new_sha":          commit["short_sha"],
            "new_message":      commit["message"],
            "new_summary":      summary,
            "previous_commits": formatted,
        })
    except Exception:
        return []

    response = response.strip()
    if response.upper().startswith("NONE"):
        return []

    found_shas = _SHA_RE.findall(response.lower())
    sha_to_entry = {e["short_sha"]: e for e in previous_entries}
    return [sha_to_entry[sha] for sha in found_shas if sha in sha_to_entry]
