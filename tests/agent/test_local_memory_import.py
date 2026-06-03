"""Import-script blockers: profile-dir resolution + fail-closed redaction."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_import_module():
    spec = importlib.util.spec_from_file_location(
        "local_memory_import_under_test", REPO_ROOT / "scripts" / "local_memory_import.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _args(**overrides):
    base = dict(storage_dir="", profile="", hermes_home="", redact=True)
    base.update(overrides)
    return argparse.Namespace(**base)


def test_named_profile_resolves_to_profiles_subdir(monkeypatch, tmp_path):
    mod = _load_import_module()
    root = tmp_path / ".hermes"
    monkeypatch.setattr(mod, "get_default_hermes_root", lambda: root)

    resolved = mod._resolve_target_storage(_args(profile="acme"))
    # Hermes profiles live at <root>/profiles/<name>, NOT ~/.hermes-<name>.
    assert resolved == root / "profiles" / "acme" / "deep-memory"


def test_default_profile_resolves_to_hermes_root(monkeypatch, tmp_path):
    mod = _load_import_module()
    root = tmp_path / ".hermes"
    monkeypatch.setattr(mod, "get_default_hermes_root", lambda: root)

    resolved = mod._resolve_target_storage(_args(profile="default"))
    assert resolved == root / "deep-memory"


def test_import_with_no_explicit_target_fails_closed(tmp_path):
    mod = _load_import_module()
    with pytest.raises(SystemExit):
        mod._resolve_target_storage(_args())


def test_import_redaction_failure_fails_closed(monkeypatch, tmp_path):
    # A forced redaction failure during import must NOT persist the raw record.
    mod = _load_import_module()

    def boom(text, *, force=False, code_file=False):
        raise RuntimeError("redaction engine exploded")

    monkeypatch.setattr("agent.redact.redact_sensitive_text", boom)

    secret = "AKIAIOSFODNN7EXAMPLE-supersecret-token"
    export = tmp_path / "drawers.jsonl"
    export.write_text(
        json.dumps({"id": "d1", "content": f"my key is {secret}", "metadata": {}}) + "\n",
        encoding="utf-8",
    )
    storage = tmp_path / "store"
    rc = mod.import_mempalace_json(_args(storage_dir=str(storage), redact=True, path=str(export)))
    assert rc == 0

    from agent.local_memory.store import LocalMemoryStore

    store = LocalMemoryStore(storage)
    # Fail-closed: the un-redactable record was skipped entirely.
    assert store.stats()["records"] == 0
    assert store.search(secret, limit=5) == []
    store.close()


def test_import_redaction_disabled_still_imports(tmp_path):
    # With redaction explicitly disabled (--no-redact), import proceeds (the user
    # opted out); only *failures* of enabled redaction fail closed.
    mod = _load_import_module()
    export = tmp_path / "drawers.jsonl"
    export.write_text(
        json.dumps({"id": "d2", "content": "ordinary non-secret legacy text", "metadata": {}}) + "\n",
        encoding="utf-8",
    )
    storage = tmp_path / "store2"
    rc = mod.import_mempalace_json(_args(storage_dir=str(storage), redact=False, path=str(export)))
    assert rc == 0

    from agent.local_memory.store import LocalMemoryStore

    store = LocalMemoryStore(storage)
    assert store.stats()["records"] == 1
    store.close()
