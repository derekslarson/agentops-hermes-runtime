from __future__ import annotations

"""Local compact-summary generation for deep-memory records.

Runs an Ollama-hosted local model (default ``gemma4:e4b-mlx``) over a verbatim
conversation-turn record to produce a short future-recall summary. This is only
ever invoked from the provider's background ingest worker -- never on the
request path -- and is strictly best-effort: any failure leaves the verbatim
record intact and the summary marked failed/skipped.
"""

import logging
import re
import subprocess
from typing import Callable

logger = logging.getLogger(__name__)

NO_SUMMARY = "NO_SUMMARY"

_PROMPT_TEMPLATE = """Summarize this Hermes conversation turn for future memory recall.

Rules:
- 1-3 concise sentences.
- Preserve durable facts, project names, file paths, decisions, commands, errors, user preferences, and implementation conclusions.
- Do not invent facts.
- Never include API keys, tokens, passwords, connection strings, or secret-like values in the summary; if a secret is relevant, write [REDACTED].
- If there is no useful durable/retrievable information, output exactly: NO_SUMMARY.

Record:
{record}
"""


def build_prompt(record_text: str) -> str:
    return _PROMPT_TEMPLATE.format(record=record_text or "")


def make_summarizer(*, model: str, backend: str = "ollama", timeout: float = 60.0) -> Callable[[str], str]:
    """Return a callable ``(record_text) -> summary_text`` for the given backend.

    The returned callable raises on backend failure (missing binary, non-zero
    exit, timeout) so the caller can record a failed summary status while
    keeping the verbatim record.
    """
    backend = (backend or "ollama").strip().lower()
    model = model or "gemma4:e4b-mlx"

    def _summarize(record_text: str) -> str:
        if backend != "ollama":
            raise RuntimeError(f"unsupported summary backend for live local memory: {backend!r}")
        return _run_ollama(record_text, model=model, timeout=timeout)

    return _summarize


def _clean_summary(text: str) -> str:
    """Normalize local model output before storing it as hint metadata."""
    out = text or ""
    # Ollama/Gemma CLI can emit terminal control sequences and line-wrap repair
    # escapes even when captured. These should never be injected into prompts.
    out = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", out)
    out = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", out)
    # Defense-in-depth: if thinking text leaks despite --hidethinking, strip it
    # rather than letting chain-of-thought pollute future memory hints.
    lowered = out.lstrip().lower()
    if lowered.startswith("thinking"):
        for marker in ("final:", "summary:", "answer:"):
            idx = lowered.rfind(marker)
            if idx >= 0:
                out = out[idx + len(marker):]
                break
        else:
            return ""
    # Force common secret-token shapes to the literal placeholder expected by
    # the memory contract even if the shared redactor abbreviates them.
    out = re.sub(r"(?i)\bsk-[A-Za-z0-9._-]{6,}\b", "[REDACTED]", out)
    out = re.sub(r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", out)
    try:
        from agent.redact import redact_sensitive_text
        out = redact_sensitive_text(out, force=True)
    except Exception:
        logger.debug("could not redact generated local-memory summary", exc_info=True)
    return out.strip()


def _run_ollama(record_text: str, *, model: str, timeout: float) -> str:
    prompt = build_prompt(record_text)
    proc = subprocess.run(
        ["ollama", "run", model, "--think", "false", "--hidethinking", "--nowordwrap"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ollama run {model} failed: {(proc.stderr or '').strip()[:200]}")
    return _clean_summary(proc.stdout or "")
