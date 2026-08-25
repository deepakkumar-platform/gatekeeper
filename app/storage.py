"""
Storage layer for the Gatekeeper gateway.

Two responsibilities:
  1. Load and watch the YAML keys file. Apps may be added, deactivated, or
     rate-limit-bumped without restarting the gateway.
  2. Append one JSON line per request to the request log for audit and
     future usage analysis.

Keys file format (managed by the gatekeeper-keys CLI, never edited by hand):

    apps:
      <app_name>:
        key_hash: <sha256 hex of the plaintext key>
        active: true | false
        rate_limit_per_min: <int>
        created_at: <ISO 8601 timestamp>
        notes: <free-form>

The plaintext key is never stored. The lookup at request time hashes the
incoming header value and compares against key_hash entries.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .config import settings


@dataclass
class AppRecord:
    app_name: str
    key_hash: str
    active: bool
    rate_limit_per_min: int
    created_at: str
    notes: str = ""
    # Models this key may call. Empty list = all models allowed (backward
    # compatible: keys issued before this feature have no restriction).
    allowed_models: list[str] = field(default_factory=list)


@dataclass
class KeysIndex:
    """Hash -> AppRecord lookup. Rebuilt whenever the file changes on disk."""

    by_hash: dict[str, AppRecord] = field(default_factory=dict)
    loaded_at: float = 0.0
    source_mtime: float = 0.0


_keys_lock = threading.RLock()
_keys_index: KeysIndex = KeysIndex()


def _hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _load_keys_file(path: str) -> KeysIndex:
    p = Path(path)
    if not p.exists():
        return KeysIndex(loaded_at=time.time(), source_mtime=0.0)

    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    index = KeysIndex(loaded_at=time.time(), source_mtime=p.stat().st_mtime)
    for app_name, cfg in (data.get("apps") or {}).items():
        key_hash = cfg.get("key_hash")
        if not key_hash or key_hash == "REPLACE_WITH_SHA256_HEX":
            continue
        index.by_hash[key_hash] = AppRecord(
            app_name=app_name,
            key_hash=key_hash,
            active=bool(cfg.get("active", True)),
            rate_limit_per_min=int(
                cfg.get("rate_limit_per_min", settings.GATEKEEPER_DEFAULT_RATE_LIMIT_PER_MIN)
            ),
            created_at=str(cfg.get("created_at", "")),
            notes=str(cfg.get("notes", "")),
            allowed_models=[str(m) for m in (cfg.get("allowed_models") or [])],
        )
    return index


def reload_if_changed() -> None:
    """Reload the keys file if its mtime has advanced. Cheap to call often."""
    global _keys_index
    path = settings.GATEKEEPER_KEYS_FILE
    try:
        mtime = Path(path).stat().st_mtime if Path(path).exists() else 0.0
    except OSError:
        return
    with _keys_lock:
        if mtime != _keys_index.source_mtime:
            _keys_index = _load_keys_file(path)


def lookup_by_plaintext(plaintext_key: str) -> Optional[AppRecord]:
    """Hash the incoming key and return the AppRecord, or None."""
    reload_if_changed()
    key_hash = _hash_key(plaintext_key)
    with _keys_lock:
        return _keys_index.by_hash.get(key_hash)


def all_apps() -> list[AppRecord]:
    reload_if_changed()
    with _keys_lock:
        return list(_keys_index.by_hash.values())


_log_lock = threading.Lock()


def append_request_log(record: dict) -> None:
    """
    Append one JSON object per line to the request log.

    Format is JSONL so it streams nicely into Promtail/Vector/Fluent later.
    Failures here are swallowed: a broken log path must not break the
    request path.
    """
    try:
        path = settings.GATEKEEPER_LOG_FILE
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        with _log_lock, open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
