"""Bounded, low-sensitivity JSONL diagnostics for provider calls.

The writer is deliberately independent from the Agent runtime.  It accepts
already summarized events, applies a second recursive redaction pass, and
keeps each stream and the whole log directory within configured limits.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterator, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


BEIJING = timezone(timedelta(hours=8))
DEFAULT_STREAMS = {
    "provider_error": "provider-errors.jsonl",
    "agent_turn": "agent-turns.jsonl",
    "context_compaction": "context-compactions.jsonl",
    "tool_turn": "tool-turns.jsonl",
}
_SENSITIVE_KEYS = {
    "api_key", "apikey", "authorization", "cookie", "password", "secret",
    "access_token", "refresh_token", "id_token", "token", "set_cookie",
}
_CONTENT_KEYS = {
    "content", "raw_content", "excerpt", "snippet", "quote", "quoted_text",
    "passage", "source_text", "raw_text", "source_excerpt", "html", "body",
    "prompt", "messages", "reasoning", "reasoning_content", "answer",
}
_SENSITIVE_QUERY_KEYS = {"key", "token", "api_key", "apikey", "authorization", "signature", "secret", "password"}
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def redact_diagnostic(value: Any, *, field_name: str | None = None) -> Any:
    """Recursively remove secrets and source-bearing fields from diagnostics."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            lowered = name.lower()
            if lowered in _SENSITIVE_KEYS or lowered in _CONTENT_KEYS:
                result[name] = "[redacted]"
            else:
                result[name] = redact_diagnostic(item, field_name=lowered)
        return result
    if isinstance(value, list):
        return [redact_diagnostic(item, field_name=field_name) for item in value[:64]]
    if isinstance(value, tuple):
        return [redact_diagnostic(item, field_name=field_name) for item in value[:64]]
    if isinstance(value, str):
        if field_name in _SENSITIVE_KEYS or field_name in _CONTENT_KEYS:
            return "[redacted]"
        if value.startswith(("http://", "https://")):
            return _redact_url(value)
    return value


class JsonlDiagnostics:
    """Thread/process-safe bounded JSONL streams used by live model clients."""

    def __init__(
        self,
        log_dir: str | os.PathLike[str] = "logs",
        *,
        file_max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 10,
        total_max_bytes: int = 500 * 1024 * 1024,
        record_max_bytes: int = 32 * 1024,
        retention_days: int | None = 14,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.file_max_bytes = max(256, int(file_max_bytes))
        self.backup_count = max(0, int(backup_count))
        self.total_max_bytes = max(self.file_max_bytes, int(total_max_bytes))
        self.record_max_bytes = min(self.file_max_bytes, max(256, int(record_max_bytes)))
        self.retention_days = None if retention_days is None else max(0, int(retention_days))

    @classmethod
    def from_env(cls) -> "JsonlDiagnostics | None":
        if os.getenv("ECHOTRACE_FILE_LOGS", "1").strip().lower() in {"0", "false", "no", "off"}:
            return None
        def integer(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, str(default)))
            except ValueError:
                return default
        retention = os.getenv("ECHOTRACE_LOG_RETENTION_DAYS", "14").strip()
        try:
            retention_value: int | None = int(retention)
        except ValueError:
            retention_value = 14
        return cls(
            os.getenv("ECHOTRACE_LOG_DIR", "logs"),
            file_max_bytes=integer("ECHOTRACE_LOG_FILE_MAX_BYTES", 10 * 1024 * 1024),
            backup_count=integer("ECHOTRACE_LOG_BACKUP_COUNT", 10),
            total_max_bytes=integer("ECHOTRACE_LOG_TOTAL_MAX_BYTES", 500 * 1024 * 1024),
            record_max_bytes=integer("ECHOTRACE_LOG_RECORD_MAX_BYTES", 32 * 1024),
            retention_days=retention_value,
        )

    def write(self, stream: str, event: Mapping[str, Any]) -> None:
        filename = DEFAULT_STREAMS.get(stream, stream if stream.endswith(".jsonl") else f"{stream}.jsonl")
        path = self.log_dir / Path(filename).name
        safe = redact_diagnostic(dict(event))
        safe.setdefault("at", datetime.now(BEIJING).isoformat(timespec="milliseconds"))
        line = self._encode_record(safe)
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with _path_lock(path):
                self._rotate_if_needed(path, len(line))
                with path.open("ab") as handle:
                    handle.write(line)
                self._cleanup()
        except Exception:
            # Diagnostics must never change Agent semantics.  The caller can
            # still expose a bounded trace counter when this boundary fails.
            return

    def _encode_record(self, value: Mapping[str, Any]) -> bytes:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        line = encoded + b"\n"
        if len(line) <= self.record_max_bytes:
            return line
        digest = hashlib.sha256(encoded).hexdigest()
        minimal = {
            "at": str(value.get("at") or "")[:40],
            "kind": str(value.get("kind", "diagnostic"))[:80],
            "record_omitted": True,
            "record_hash": f"sha256:{digest}",
        }
        line = json.dumps(minimal, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(line) <= self.record_max_bytes:
            return line
        # The configured minimum is sufficient for this fixed fallback. Keep
        # a valid JSON object even if an unusually small custom limit is used.
        fallback = b'{"kind":"diagnostic","record_omitted":true}\n'
        return fallback if len(fallback) <= self.record_max_bytes else b"{}\n"

    def _rotate_if_needed(self, path: Path, incoming_bytes: int) -> None:
        if not path.exists() or path.stat().st_size + incoming_bytes <= self.file_max_bytes:
            return
        if self.backup_count == 0:
            path.unlink(missing_ok=True)
            return
        for index in range(self.backup_count, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            target = path.with_name(f"{path.name}.{index + 1}")
            if index == self.backup_count and source.exists():
                source.unlink(missing_ok=True)
            elif source.exists():
                os.replace(source, target)
        if path.exists():
            os.replace(path, path.with_name(f"{path.name}.1"))

    def _cleanup(self) -> None:
        files = _log_files(self.log_dir)
        now = time.time()
        if self.retention_days is not None:
            cutoff = now - self.retention_days * 86400
            for item in files:
                if item.stat().st_mtime < cutoff:
                    item.unlink(missing_ok=True)
        files = _log_files(self.log_dir)
        total = sum(item.stat().st_size for item in files)
        for item in sorted(files, key=lambda current: current.stat().st_mtime):
            if total <= self.total_max_bytes:
                break
            size = item.stat().st_size
            item.unlink(missing_ok=True)
            total -= size


@contextmanager
def _path_lock(path: Path) -> Iterator[None]:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:
        lock_path = path.with_name(path.name + ".lock")
        acquired = False
        while True:
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(descriptor)
                acquired = True
                break
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > 60:
                        lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                time.sleep(0.01)
        try:
            yield
        finally:
            if acquired:
                lock_path.unlink(missing_ok=True)


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        query = []
        for key, item in parse_qsl(parsed.query, keep_blank_values=True):
            query.append((key, "[redacted]" if key.lower() in _SENSITIVE_QUERY_KEYS else item))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    except Exception:
        return value


def _log_files(directory: Path) -> list[Path]:
    files = []
    for item in directory.glob("*.jsonl*"):
        if not item.is_file() or item.name.endswith(".lock"):
            continue
        suffix = item.name.rsplit(".jsonl", 1)[-1]
        if suffix == "" or (suffix.startswith(".") and suffix[1:].isdigit()):
            files.append(item)
    return files


__all__ = ["JsonlDiagnostics", "redact_diagnostic"]
