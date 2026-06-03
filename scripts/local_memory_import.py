#!/usr/bin/env python3
"""Import legacy/deep-memory sources into the flat local Hermes memory store.

This importer never deletes source data. It stores source text verbatim and maps
old palace wing/room values to flat legacy_wing/legacy_room metadata only.

Tenant safety: bulk history can silently contaminate the wrong org/user/project
if it lands in an ambiguous store. This importer therefore REQUIRES an explicit
target scope/profile on the command line — ``--storage-dir`` (an exact store
path), ``--profile``/``--hermes-home`` (a named profile home), or both. It will
refuse to run against an ambient/default location inferred only from the
environment.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.local_memory.provider import (
    LocalDeepMemoryProvider,
    RedactionError,
    _redact,
    load_deep_memory_config,
    resolve_storage_dir,
)
from agent.local_memory.store import LocalMemoryStore, stable_record_id
from hermes_constants import get_default_hermes_root, get_hermes_home


def extract_text(content: Any) -> str:
    """Best-effort plain-text extraction from a message ``content`` field.

    Mirrors the shape of Hermes transcript content (a string, or a list of
    OpenAI-style content blocks). Kept local so the importer does not hard-depend
    on optional transcript-archive helpers.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(p for p in parts if p)
    return str(content)


def _redact_or_skip(text: str, enabled: bool) -> Optional[str]:
    """Redact, failing CLOSED.

    Returns the redacted text, or ``None`` when redaction is enabled but fails —
    in which case the caller must skip the record/turn rather than persist the
    raw (possibly secret-bearing) text. With redaction disabled (``--no-redact``)
    the user has explicitly opted out, so the original text is returned.
    """
    try:
        return _redact(text, enabled)
    except RedactionError:
        return None


def _epoch(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return float(text)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return time.mktime(time.strptime(text.replace("+00:00", "Z"), fmt))
            except ValueError:
                continue
    return None


def _iter_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def _iter_paths(root: Path, suffixes: tuple[str, ...]) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in suffixes:
            yield path


def _resolve_target_storage(args: argparse.Namespace) -> Path:
    """Resolve the explicit target store path, or fail closed.

    Requires an explicit scope/profile so a bulk import can never contaminate an
    ambiguous or default store inferred only from the ambient environment.
    """
    if args.storage_dir:
        return Path(args.storage_dir).expanduser()
    if args.profile or args.hermes_home:
        if args.hermes_home:
            home = Path(args.hermes_home).expanduser()
        elif args.profile in ("", "default"):
            # Default profile lives at the Hermes root (e.g. ~/.hermes).
            home = Path(get_default_hermes_root())
        else:
            # Named profiles live at <root>/profiles/<name>, NOT ~/.hermes-<name>.
            home = Path(get_default_hermes_root()) / "profiles" / args.profile
        cfg = load_deep_memory_config()
        return resolve_storage_dir(str(cfg.get("storage_dir")), str(home))
    raise SystemExit(
        "error: refusing to import without an explicit target scope/profile. "
        "Pass --storage-dir <path> (exact store) or --profile/--hermes-home <name> "
        "so bulk history cannot contaminate the wrong org/user/project store."
    )


def open_store(args: argparse.Namespace) -> LocalMemoryStore:
    storage = _resolve_target_storage(args)
    cfg = load_deep_memory_config()
    return LocalMemoryStore(
        storage,
        embedding_provider=str(cfg.get("embedding_provider") or "onnx-minilm"),
        embedding_model=str(cfg.get("embedding_model") or "all-MiniLM-L6-v2"),
        embedding_device=str(cfg.get("embedding_device") or "auto"),
    )


def _summary_provider(store: LocalMemoryStore, config: Optional[Dict[str, Any]] = None) -> LocalDeepMemoryProvider:
    provider = LocalDeepMemoryProvider({"deep_memory": config or load_deep_memory_config()})
    provider.store = store
    return provider


def _summarize_if_needed(store: LocalMemoryStore, record_id: str, text: str, cfg: Dict[str, Any]) -> None:
    """Apply the same summary/short-record gating metadata used by live sync."""
    existing = store.get_record(record_id)
    if existing and existing.summary_status in {"ready", "skipped", "failed"}:
        return
    _summary_provider(store, cfg)._summarize_record(record_id, text)


def import_transcripts(args: argparse.Namespace) -> int:
    store = open_store(args)
    cfg = load_deep_memory_config()
    count = 0
    try:
        for path in _iter_paths(Path(args.path).expanduser(), (".jsonl",)):
            for obj in _iter_jsonl(path):
                hermes_obj = obj.get("hermes")
                hermes: Dict[str, Any] = hermes_obj if isinstance(hermes_obj, dict) else {}
                message_obj = obj.get("message")
                message: Dict[str, Any] = message_obj if isinstance(message_obj, dict) else {}
                role = hermes.get("role") or obj.get("type") or message.get("role") or "unknown"
                text = extract_text(message.get("content"))
                if not text.strip():
                    continue
                text = _redact_or_skip(text, args.redact)
                if text is None:
                    continue
                source_uri = f"file://{path}#{hermes.get('dedup_key') or stable_record_id(path, role, text)}"
                if store.is_ingested(source_uri):
                    continue
                rec = store.upsert_record(
                    text=text,
                    metadata={
                        "type": "transcript_message",
                        "record_kind": "transcript_message",
                        "session_id": hermes.get("session_id"),
                        "message_ids": [hermes.get("db_message_id")] if hermes.get("db_message_id") is not None else [],
                        "role": role,
                    },
                    source=hermes.get("source") or "transcript_archive",
                    source_uri=source_uri,
                    timestamp=_epoch(hermes.get("epoch") or obj.get("timestamp")),
                    record_kind="transcript_message",
                    provenance={"importer": "local_memory_import.py transcript", "source_file": str(path), "hermes": hermes},
                )
                _summarize_if_needed(store, rec.id, text, cfg)
                store.mark_ingested(source_uri, rec.id, source_uri)
                count += 1
    finally:
        store.close()
    print(f"imported {count} transcript records")
    return 0


def _drawer_text(obj: Dict[str, Any]) -> str:
    for key in ("content", "text", "document", "page_content", "verbatim"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def import_mempalace_json(args: argparse.Namespace) -> int:
    """Import JSON/JSONL exports of MemPalace drawers.

    Accepts objects with common fields: content/text, wing, room, drawer_id/id,
    source_file/source_uri, metadata. Old wing/room are preserved only as flat
    legacy metadata fields.
    """
    store = open_store(args)
    cfg = load_deep_memory_config()
    count = 0
    try:
        for path in _iter_paths(Path(args.path).expanduser(), (".jsonl", ".json")):
            if path.suffix.lower() == ".jsonl":
                objs = _iter_jsonl(path)
            else:
                raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(raw, dict) and isinstance(raw.get("drawers"), list):
                    objs = iter(raw["drawers"])
                elif isinstance(raw, list):
                    objs = iter(raw)
                else:
                    objs = iter([raw] if isinstance(raw, dict) else [])
            for obj in objs:
                if not isinstance(obj, dict):
                    continue
                text = _drawer_text(obj)
                if not text.strip():
                    continue
                text = _redact_or_skip(text, args.redact)
                if text is None:
                    continue
                md_obj = obj.get("metadata")
                md: Dict[str, Any] = md_obj if isinstance(md_obj, dict) else {}
                legacy_wing = obj.get("wing") or md.get("wing")
                legacy_room = obj.get("room") or md.get("room")
                flat_md: Dict[str, Any] = dict(md)
                if legacy_wing:
                    flat_md["legacy_wing"] = legacy_wing
                if legacy_room:
                    flat_md["legacy_room"] = legacy_room
                flat_md.setdefault("type", "mempalace_legacy_drawer")
                flat_md.setdefault("record_kind", "legacy_import")
                flat_md.pop("wing", None)
                flat_md.pop("room", None)
                drawer_id = obj.get("drawer_id") or obj.get("id") or stable_record_id(path, text)
                source_uri = obj.get("source_uri") or obj.get("source_file") or md.get("source_file") or f"mempalace://drawer/{drawer_id}"
                event = f"mempalace-json:{drawer_id}:{source_uri}"
                if store.is_ingested(event):
                    continue
                rec = store.upsert_record(
                    text=text,
                    metadata=flat_md,
                    source="mempalace_legacy_import",
                    source_uri=str(source_uri),
                    timestamp=_epoch(obj.get("timestamp") or md.get("timestamp") or obj.get("created_at")),
                    record_kind="legacy_import",
                    provenance={"importer": "local_memory_import.py mempalace-json", "source_file": str(path), "legacy_drawer_id": drawer_id},
                )
                _summarize_if_needed(store, rec.id, text, cfg)
                store.mark_ingested(event, rec.id, str(source_uri))
                count += 1
    finally:
        store.close()
    print(f"imported {count} MemPalace legacy records")
    return 0


def _message_text(message: Dict[str, Any]) -> str:
    return extract_text(message.get("content"))


def _iter_completed_turns(messages: Iterable[Dict[str, Any]]) -> Iterator[tuple[Dict[str, Any], Dict[str, Any], str, str]]:
    pending_user: Optional[Dict[str, Any]] = None
    pending_user_text = ""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").lower()
        if role == "user":
            text = _message_text(msg)
            if text.strip():
                pending_user = msg
                pending_user_text = text
            continue
        if role != "assistant" or not pending_user:
            continue
        assistant_text = _message_text(msg)
        # Tool-call-only assistant messages are not completed user-facing turns.
        if not assistant_text.strip():
            continue
        yield pending_user, msg, pending_user_text, assistant_text
        pending_user = None
        pending_user_text = ""


def _apply_summary_result(store: LocalMemoryStore, record_id: str, summary: str, cfg: Dict[str, Any]) -> str:
    backend = str(cfg.get("summary_backend") or "ollama").strip().lower()
    model = str(cfg.get("summary_model") or ("claude-haiku-4-5" if backend == "anthropic" else "gemma4:e4b-mlx"))
    cleaned = (summary or "").strip()
    if not cleaned or cleaned.upper() == "NO_SUMMARY":
        store.update_record_summary(record_id, "", status="skipped", model=model, version="v1")
        return "skipped"
    max_chars = int(cfg.get("summary_max_chars") or 700)
    store.update_record_summary(record_id, cleaned[:max_chars], status="ready", model=model, version="v1")
    return "ready"


def _env_value_from_file(path: Path, key: str) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() != key:
                continue
            value = value.strip()
            if (value.startswith('\"') and value.endswith('\"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            return value
    except Exception:
        return ""
    return ""


def _anthropic_api_key() -> str:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    key = _env_value_from_file(get_hermes_home() / ".env", "ANTHROPIC_API_KEY").strip()
    if key:
        os.environ.setdefault("ANTHROPIC_API_KEY", key)
    return key


def _openrouter_api_key() -> str:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    key = _env_value_from_file(get_hermes_home() / ".env", "OPENROUTER_API_KEY").strip()
    if key:
        os.environ.setdefault("OPENROUTER_API_KEY", key)
    return key


def _run_script_anthropic_summary(record_text: str, *, model: str, timeout: float) -> str:
    """Script-only Anthropic summary call for bulk imports.

    This intentionally does not live in make_summarizer(), so normal Hermes
    background turn sync keeps using the configured local Gemma/Ollama path.
    """
    from agent.local_memory.summarizer import _clean_summary, build_prompt

    api_key = _anthropic_api_key()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    payload = {
        "model": model or "claude-haiku-4-5",
        "max_tokens": 220,
        "temperature": 0,
        "messages": [{"role": "user", "content": build_prompt(record_text)}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    last_error = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            last_error = RuntimeError(f"anthropic summary request failed: HTTP {exc.code}: {detail}")
            if exc.code not in {408, 409, 429, 500, 502, 503, 504, 529} or attempt == 4:
                raise last_error from exc
            time.sleep(min(30.0, 1.5 * (2 ** attempt)))
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f"anthropic summary request failed: {exc}")
            if attempt == 4:
                raise last_error from exc
            time.sleep(min(30.0, 1.5 * (2 ** attempt)))
    else:
        raise last_error or RuntimeError("anthropic summary request failed")
    data = json.loads(body)
    parts: list[str] = []
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return _clean_summary("\n".join(parts))


def _run_script_openrouter_summary(record_text: str, *, model: str, timeout: float) -> str:
    """Script-only OpenRouter summary call for bulk imports.

    Kept out of make_summarizer(), so normal Hermes background turn sync keeps
    using the configured local Gemma/Ollama path.
    """
    from agent.local_memory.summarizer import _clean_summary, build_prompt

    api_key = _openrouter_api_key()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    payload = {
        "model": model or "google/gemini-3.1-flash-lite",
        "messages": [{"role": "user", "content": build_prompt(record_text)}],
        "temperature": 0,
        "max_tokens": 220,
    }
    body = ""
    last_error = None
    for attempt in range(5):
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/NousResearch/hermes-agent",
                "X-Title": "Hermes Deep Memory Import",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            last_error = RuntimeError(f"openrouter summary request failed: HTTP {exc.code}: {detail}")
            if exc.code not in {408, 409, 429, 500, 502, 503, 504, 529} or attempt == 4:
                raise last_error from exc
            time.sleep(min(30.0, 1.5 * (2 ** attempt)))
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f"openrouter summary request failed: {exc}")
            if attempt == 4:
                raise last_error from exc
            time.sleep(min(30.0, 1.5 * (2 ** attempt)))
    else:
        raise last_error or RuntimeError("openrouter summary request failed")
    data = json.loads(body)
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    return _clean_summary(str((message or {}).get("content") or ""))


def _summarize_batch(store: LocalMemoryStore, items: list[tuple[str, str]], cfg: Dict[str, Any], *, concurrency: int) -> Dict[str, int]:
    """Summarize already-upserted records with concurrent model calls.

    Chroma writes stay on the main thread; only remote/local summary calls run in
    parallel. Short records are marked skipped without spending a model call.
    """
    stats = {"ready": 0, "skipped": 0, "failed": 0}
    if not items or not bool(cfg.get("summarize_records", True)):
        return stats
    backend = str(cfg.get("summary_backend") or "ollama").strip().lower()
    default_model = "claude-haiku-4-5" if backend == "anthropic" else ("google/gemini-3.1-flash-lite" if backend == "openrouter" else "gemma4:e4b-mlx")
    model = str(cfg.get("summary_model") or default_model)
    min_chars = int(cfg.get("summary_min_chars") or 0)
    pending: list[tuple[str, str]] = []
    for rid, text in items:
        if min_chars > 0 and len((text or "").strip()) < min_chars:
            store.update_record_summary(rid, "", status="skipped", model=model, version="v1")
            stats["skipped"] += 1
        else:
            pending.append((rid, text))
    if not pending:
        return stats
    if backend == "anthropic":
        summarizer = lambda text: _run_script_anthropic_summary(
            text,
            model=str(cfg.get("summary_model") or "claude-haiku-4-5"),
            timeout=float(cfg.get("summary_timeout_seconds") or 60),
        )
    elif backend == "openrouter":
        summarizer = lambda text: _run_script_openrouter_summary(
            text,
            model=str(cfg.get("summary_model") or "google/gemini-3.1-flash-lite"),
            timeout=float(cfg.get("summary_timeout_seconds") or 60),
        )
    else:
        from agent.local_memory.summarizer import make_summarizer

        summarizer = make_summarizer(
            model=model,
            backend=backend,
            timeout=float(cfg.get("summary_timeout_seconds") or 60),
        )
    with ThreadPoolExecutor(max_workers=max(1, int(concurrency or 1))) as pool:
        futures = {pool.submit(summarizer, text): rid for rid, text in pending}
        for fut in as_completed(futures):
            rid = futures[fut]
            try:
                status = _apply_summary_result(store, rid, fut.result(), cfg)
                stats[status] = stats.get(status, 0) + 1
            except Exception:
                store.update_record_summary(rid, "", status="failed", model=model, version="v1")
                stats["failed"] += 1
    return stats


def import_sessions_export(args: argparse.Namespace) -> int:
    """Import `hermes sessions export` JSONL as completed conversation turns."""
    store = open_store(args)
    cfg = load_deep_memory_config()
    if args.summary_backend:
        cfg["summary_backend"] = args.summary_backend
        if args.summary_backend == "anthropic" and not args.summary_model:
            cfg["summary_model"] = "claude-haiku-4-5"
        elif args.summary_backend == "openrouter" and not args.summary_model:
            cfg["summary_model"] = "google/gemini-3.1-flash-lite"
    if args.summary_model:
        cfg["summary_model"] = args.summary_model
    if args.summary_timeout_seconds:
        cfg["summary_timeout_seconds"] = args.summary_timeout_seconds
    batch_size = max(1, int(args.batch_size or 1))
    concurrency = max(1, int(args.summary_concurrency or 1))
    include_platforms = {p.strip().lower() for p in str(args.include_platforms or "telegram").split(",") if p.strip()}
    include_all_platforms = "all" in include_platforms or "*" in include_platforms
    count = 0
    skipped_platform = 0
    skipped_existing = 0
    resummarized_existing = 0
    summary_totals = {"ready": 0, "skipped": 0, "failed": 0}
    pending: list[tuple[str, str, str]] = []
    started = time.time()

    def flush() -> None:
        nonlocal pending, summary_totals
        if not pending:
            return
        batch = pending
        pending = []
        stats = _summarize_batch(store, [(rid, text) for rid, text, _source_uri in batch], cfg, concurrency=concurrency)
        for key, val in stats.items():
            summary_totals[key] = summary_totals.get(key, 0) + val
        for rid, _text, source_uri in batch:
            store.mark_ingested(source_uri, rid, source_uri)
        elapsed = max(0.001, time.time() - started)
        print(
            "progress "
            f"imported={count} skipped_platform={skipped_platform} skipped_existing={skipped_existing} resummarized_existing={resummarized_existing} "
            f"ready={summary_totals.get('ready', 0)} skipped={summary_totals.get('skipped', 0)} failed={summary_totals.get('failed', 0)} "
            f"elapsed={elapsed:.1f}s rate={count / elapsed:.2f}/s",
            flush=True,
        )

    try:
        for path in _iter_paths(Path(args.path).expanduser(), (".jsonl",)):
            for session in _iter_jsonl(path):
                messages = session.get("messages")
                if not isinstance(messages, list):
                    continue
                sid = str(session.get("id") or "")
                platform = str(session.get("source") or "").strip().lower()
                if include_platforms and not include_all_platforms and platform not in include_platforms:
                    skipped_platform += len(list(_iter_completed_turns(messages)))
                    continue
                for user_msg, assistant_msg, user_text, assistant_text in _iter_completed_turns(messages):
                    user_text = _redact_or_skip(user_text, args.redact)
                    assistant_text = _redact_or_skip(assistant_text, args.redact)
                    if user_text is None or assistant_text is None:
                        # Fail closed: skip turns we could not redact.
                        continue
                    if not user_text.strip() or not assistant_text.strip():
                        continue
                    text = f"User:\n{user_text}\n\nAssistant:\n{assistant_text}"
                    source_uri = f"hermes://session/{sid}/turn/{stable_record_id(sid, user_text, assistant_text)[4:16]}"
                    rid = stable_record_id(source_uri, "conversation_turn", "", text)
                    existing = store.get_record(rid)
                    if existing and existing.summary_status in {"ready", "skipped", "failed"}:
                        if args.resummarize_existing:
                            pending.append((existing.id, existing.text, source_uri))
                            resummarized_existing += 1
                            if len(pending) >= batch_size:
                                flush()
                        else:
                            skipped_existing += 1
                        continue
                    rec = store.upsert_record(
                        text=text,
                        metadata={
                            "type": "conversation_turn",
                            "record_kind": "conversation_turn",
                            "session_id": sid,
                            "session_title": session.get("title"),
                            "platform": platform or session.get("source"),
                            "message_ids": [user_msg.get("id"), assistant_msg.get("id")],
                        },
                        source="conversation_turn",
                        source_uri=source_uri,
                        timestamp=_epoch(assistant_msg.get("timestamp") or user_msg.get("timestamp") or session.get("last_active")),
                        record_kind="conversation_turn",
                        provenance={
                            "importer": "local_memory_import.py sessions-export",
                            "source_file": str(path),
                            "session_id": sid,
                            "user_message_id": user_msg.get("id"),
                            "assistant_message_id": assistant_msg.get("id"),
                        },
                        record_id=rid,
                    )
                    pending.append((rec.id, text, source_uri))
                    count += 1
                    if len(pending) >= batch_size:
                        flush()
        flush()
    finally:
        store.close()
    print(
        f"imported {count} session-export turns; skipped {skipped_platform} turns outside selected platforms; "
        f"skipped {skipped_existing} existing summarized turns; resummarized {resummarized_existing} existing turns; "
        f"summaries ready={summary_totals.get('ready', 0)} skipped={summary_totals.get('skipped', 0)} failed={summary_totals.get('failed', 0)}"
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", default="", help="Explicit Hermes profile home (target scope)")
    parser.add_argument("--profile", default="", help="Explicit profile name (target scope); resolves ~/.hermes-<profile>")
    parser.add_argument("--storage-dir", default="", help="Explicit flat memory storage dir (exact target store)")
    parser.add_argument("--no-redact", dest="redact", action="store_false", help="Disable secret redaction on imported text (redaction is on by default)")
    parser.set_defaults(redact=True)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_tx = sub.add_parser("transcript", help="Import Hermes transcript-archive JSONL")
    p_tx.add_argument("path", help="Transcript archive file or directory")
    p_tx.set_defaults(func=import_transcripts)
    p_mp = sub.add_parser("mempalace-json", help="Import JSON/JSONL MemPalace drawer exports")
    p_mp.add_argument("path", help="Export file or directory")
    p_mp.set_defaults(func=import_mempalace_json)
    p_sx = sub.add_parser("sessions-export", help="Import `hermes sessions export` JSONL as conversation turns")
    p_sx.add_argument("path", help="Sessions export JSONL file or directory")
    p_sx.add_argument("--batch-size", type=int, default=25, help="Number of imported turns to summarize per flush")
    p_sx.add_argument("--summary-concurrency", type=int, default=25, help="Concurrent summary calls for this script only")
    p_sx.add_argument("--summary-backend", choices=("ollama", "anthropic", "openrouter"), default="", help="Override summary backend for this import only")
    p_sx.add_argument("--summary-model", default="", help="Override summary model for this import only")
    p_sx.add_argument("--summary-timeout-seconds", type=int, default=0, help="Override per-summary timeout for this import only")
    p_sx.add_argument("--include-platforms", default="telegram", help="Comma-separated session sources to import; default telegram. Use 'all' to import every source.")
    p_sx.add_argument("--resummarize-existing", action="store_true", help="Recompute summaries for existing session-export records instead of skipping them")
    p_sx.set_defaults(func=import_sessions_export)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
