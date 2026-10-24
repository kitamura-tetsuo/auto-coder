"""Bounded JSON Lines logging for LLM interactions."""

from __future__ import annotations

import json
import os
import re
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .review_capture.context import get_active_interaction_id, get_active_review_context
from .runtime_locks import file_lock
from .security_utils import redact_data, redact_string

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
MIN_MAX_BYTES = 4096
MAX_COUNTER = 2**63 - 1
LOCK_TIMEOUT_SECONDS = 5.0


class LLMOutputLogger:
    """Write redacted LLM interactions to a bounded rotating JSONL family."""

    def __init__(self, log_path: Optional[Union[str, Path]] = None, enabled: Optional[bool] = None, environment_variable: str = "AUTO_CODER_LLM_OUTPUT_LOG_ENABLED"):
        self.env_var = environment_variable
        value = os.environ.get(self.env_var, "").strip().lower()
        self.enabled = value in {"1", "true", "yes", "on"} if value else (enabled if enabled is not None else True)
        self.log_path = Path(log_path) if log_path is not None else Path.home() / ".auto-coder/logs/llm_output.jsonl"
        self.max_bytes = self._setting("AUTO_CODER_LLM_OUTPUT_LOG_MAX_BYTES", DEFAULT_MAX_BYTES, MIN_MAX_BYTES)
        self.backup_count = self._setting("AUTO_CODER_LLM_OUTPUT_LOG_BACKUP_COUNT", DEFAULT_BACKUP_COUNT, 0)
        # Compatibility only: append descriptors are intentionally never cached.
        self._file_handle: Optional[Any] = None

    @staticmethod
    def _diagnostic(message: str) -> None:
        print(f"Warning: LLM output log: {message[:500]}", file=sys.stderr)

    @classmethod
    def _setting(cls, name: str, default: int, minimum: int) -> int:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            result = int(raw)
            if result < minimum:
                raise ValueError
            return result
        except (TypeError, ValueError):
            cls._diagnostic(f"invalid {name}; using default {default}")
            return default

    def _is_enabled(self) -> bool:
        value = os.environ.get(self.env_var, "").strip().lower()
        return value in {"1", "true", "yes", "on"} if value else self.enabled

    @staticmethod
    def _get_timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _paths(self) -> tuple[Path, Path, Path]:
        active = self.log_path.expanduser().resolve()
        return active, active.with_name(active.name + ".auto-coder.lock"), active.with_name(active.name + ".auto-coder-stage.jsonl")

    @staticmethod
    def _secure_open(path: Path, flags: int) -> int:
        fd = os.open(path, flags | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        return fd

    def _archives(self, active: Path) -> list[tuple[int, Path]]:
        pattern = re.compile(rf"^{re.escape(active.name)}\.auto-coder-archive\.(\d{{20,}})\.jsonl$")
        found = []
        for candidate in active.parent.iterdir():
            match = pattern.fullmatch(candidate.name)
            if match and candidate.is_file():
                found.append((int(match.group(1)), candidate))
        return sorted(found)

    def _serialize(self, data: Dict[str, Any], supplied_lengths: Optional[Dict[str, Any]] = None) -> bytes:
        try:
            ordinary = (json.dumps(data, ensure_ascii=False) + "\n").encode()
        except Exception as exc:
            raise ValueError(f"record serialization failed ({type(exc).__name__})") from exc
        if len(ordinary) <= self.max_bytes:
            return ordinary
        summary: Dict[str, Any] = {"log_record_truncated": True, "original_record_bytes": len(ordinary), "omitted_length_fields": []}
        lengths = supplied_lengths if supplied_lengths is not None else data
        for name in ("prompt_length", "response_length"):
            value = lengths.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_COUNTER:
                summary[name] = value
            elif name in lengths:
                summary["omitted_length_fields"].append(name)
        for name in ("timestamp", "event_type", "backend", "model", "status"):
            value = data.get(name)
            if isinstance(value, (str, int, float, bool)) or value is None:
                candidate = dict(summary)
                candidate[name] = value[:512] if isinstance(value, str) else value
                encoded = (json.dumps(candidate, ensure_ascii=False) + "\n").encode()
                if len(encoded) <= self.max_bytes:
                    summary = candidate
        result = (json.dumps(summary, ensure_ascii=False) + "\n").encode()
        if len(result) > self.max_bytes:
            raise ValueError("truncated summary exceeds configured bound")
        return result

    def _normalize(self, path: Path, stage: Path) -> None:
        """Keep the newest complete JSON object lines using bounded memory."""
        if not path.exists():
            return
        retained: deque[bytes] = deque()
        size = 0
        pending = bytearray()
        discarding = False
        with path.open("rb", buffering=64 * 1024) as source:
            while chunk := source.read(64 * 1024):
                for part in chunk.splitlines(keepends=True):
                    if discarding:
                        if part.endswith(b"\n"):
                            discarding = False
                        continue
                    pending.extend(part)
                    if len(pending) > self.max_bytes:
                        pending.clear()
                        discarding = not part.endswith(b"\n")
                        continue
                    if not pending.endswith(b"\n"):
                        continue
                    line = bytes(pending)
                    pending.clear()
                    try:
                        valid = isinstance(json.loads(line.decode("utf-8")), dict)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        valid = False
                    if valid:
                        retained.append(line)
                        size += len(line)
                        while size > self.max_bytes:
                            size -= len(retained.popleft())
        fd = self._secure_open(stage, os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(fd, "wb") as output:
            output.writelines(retained)
            output.flush()
            os.fsync(output.fileno())
        os.replace(stage, path)
        os.chmod(path, 0o600)

    def _maintain_and_append(self, line: bytes) -> None:
        active, lock_path, stage = self._paths()
        active.parent.mkdir(parents=True, exist_ok=True)

        def open_lock() -> int:
            return self._secure_open(lock_path, os.O_RDWR)

        with file_lock(lock_path, opener=open_lock, timeout=LOCK_TIMEOUT_SECONDS):
            stage.unlink(missing_ok=True)
            for _, path in self._archives(active):
                self._normalize(path, stage)
            self._normalize(active, stage)
            archives = self._archives(active)
            while len(archives) > self.backup_count:
                archives.pop(0)[1].unlink()
            if (active.stat().st_size if active.exists() else 0) + len(line) > self.max_bytes:
                if self.backup_count:
                    generation = archives[-1][0] + 1 if archives else 0
                    archive = active.with_name(f"{active.name}.auto-coder-archive.{generation:020d}.jsonl")
                    if active.exists():
                        os.replace(active, archive)
                        os.chmod(archive, 0o600)
                    archives = self._archives(active)
                    while len(archives) > self.backup_count:
                        archives.pop(0)[1].unlink()
                else:
                    active.unlink(missing_ok=True)
            fd = self._secure_open(active, os.O_WRONLY | os.O_APPEND)
            with os.fdopen(fd, "ab") as output:
                output.write(line)
                output.flush()
                os.fsync(output.fileno())
            stage.unlink(missing_ok=True)

    def _write_json_line(self, data: Dict[str, Any], supplied_lengths: Optional[Dict[str, Any]] = None) -> None:
        if not self._is_enabled():
            return
        ctx = get_active_review_context()
        correlation = ("review_id", "interaction_id", "repository", "target_type", "target_number", "review_kind", "generation")
        if ctx is None:
            for name in correlation:
                data.pop(name, None)
        else:
            data.update(review_id=ctx.review_id, repository=ctx.repository, target_type=ctx.target_type, target_number=ctx.target_number, review_kind=ctx.review_kind, generation=ctx.generation_identity)
            interaction_id = get_active_interaction_id()
            if interaction_id:
                data["interaction_id"] = interaction_id
        data.setdefault("timestamp", self._get_timestamp())
        try:
            self._maintain_and_append(self._serialize(data, supplied_lengths))
        except Exception as exc:
            self._diagnostic(f"write skipped ({type(exc).__name__}): {str(exc)[:250]}")

    def log_request(self, backend: str, model: Optional[str] = None, prompt: Optional[str] = None, prompt_length: Optional[int] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        data: Dict[str, Any] = {"event_type": "llm_request", "backend": backend}
        if model is not None:
            data["model"] = model
        if prompt is not None:
            data["prompt_length"] = len(prompt)
        elif prompt_length is not None:
            data["prompt_length"] = prompt_length
        supplied = {"prompt_length": data["prompt_length"]} if "prompt_length" in data else {}
        if metadata:
            data.update(redact_data(metadata))
        self._write_json_line(data, supplied)

    def log_response(self, backend: str, model: Optional[str] = None, response: Optional[str] = None, response_length: Optional[int] = None, duration_ms: Optional[float] = None, status: str = "success", error: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        data: Dict[str, Any] = {"event_type": "llm_response", "backend": backend, "status": status}
        if model is not None:
            data["model"] = model
        if response is not None:
            data.update(response_length=len(response), response=redact_string(response))
        elif response_length is not None:
            data["response_length"] = response_length
        if duration_ms is not None:
            data["duration_ms"] = duration_ms
        if error is not None:
            data["error"] = redact_string(error)
        supplied = {"response_length": data["response_length"]} if "response_length" in data else {}
        if metadata:
            data.update(redact_data(metadata))
        self._write_json_line(data, supplied)

    def log_interaction(self, backend: str, model: Optional[str] = None, prompt: Optional[str] = None, response: Optional[str] = None, duration_ms: Optional[float] = None, status: str = "success", error: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        data: Dict[str, Any] = {"event_type": "llm_interaction", "backend": backend, "status": status}
        if model is not None:
            data["model"] = model
        if prompt is not None:
            data["prompt_length"] = len(prompt)
        if response is not None:
            data.update(response_length=len(response), response=redact_string(response))
        if duration_ms is not None:
            data["duration_ms"] = duration_ms
        if error is not None:
            data["error"] = redact_string(error)
        supplied: Dict[str, Any] = {name: data[name] for name in ("prompt_length", "response_length") if name in data}
        if metadata:
            data.update(redact_data(metadata))
        self._write_json_line(data, supplied)

    def flush(self) -> None:
        """Writes are already flushed synchronously."""

    def close(self) -> None:
        """No descriptor is retained."""

    def _close_file(self) -> None:
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __repr__(self) -> str:
        return f"LLMOutputLogger(enabled={self.enabled}, path={self.log_path}, env_var={self.env_var})"
