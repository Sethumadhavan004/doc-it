"""
noir.py — Local LLM factory for noir mode.

Constructs a ChatOpenAI instance pointed at any OpenAI-compatible local server
(LM Studio, Ollama, vLLM, etc.). Called only when `doc-it run --mode noir` is used.
"""

import urllib.request
import urllib.error

from langchain_openai import ChatOpenAI

_REQUIRED_KEYS = {"local_llm_url", "local_llm_model", "temperature"}


def _check_server_reachable(url: str) -> bool:
    """
    Sends a HEAD request to the base URL.
    Returns False only on connection refused / timeout (server not started).
    Any HTTP response (even 404/405) means the server is up.
    """
    try:
        urllib.request.urlopen(
            urllib.request.Request(url, method="HEAD"), timeout=3
        )
        return True
    except urllib.error.URLError:
        return False
    except Exception:
        return True


def make_noir_llm(config: dict) -> ChatOpenAI:
    """
    Returns a ChatOpenAI instance pointed at the local LLM server.

    Raises ValueError (not ClickException) so cli.py can wrap it with context.
    """
    missing = _REQUIRED_KEYS - config.keys()
    if missing:
        raise ValueError(
            f"Noir config missing fields: {', '.join(sorted(missing))}. "
            "Run `doc-it noir setup` to reconfigure."
        )

    url   = config["local_llm_url"]
    model = config["local_llm_model"]
    temp  = float(config.get("temperature", 0.2))

    if not _check_server_reachable(url):
        raise ValueError(
            f"Cannot reach local LLM server at {url}.\n"
            "Make sure LM Studio / Ollama is running before using --mode noir.\n"
            "Reconfigure the URL with `doc-it noir setup`."
        )

    return ChatOpenAI(
        base_url=url,
        api_key="not-needed",
        model=model,
        temperature=temp,
    )
