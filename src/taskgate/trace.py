"""JSONL traces of one agent task."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


class TaskgateError(Exception):
    """The trace or policy could not be read, so the check did not run."""


@dataclass(frozen=True)
class Event:
    kind: str
    data: dict[str, Any]
    line: int


_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens")
_INT_FIELDS = _TOKEN_FIELDS + ("latency_ms",)


def load_trace(path: Path) -> list[Event]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TaskgateError(f"{path}: {exc.strerror}") from exc
    if not text.strip():
        raise TaskgateError(f"{path}: trace is empty")
    stripped = text.lstrip()
    if stripped.startswith("["):
        return _events_from_array(path, text)
    return _events_from_jsonl(path, text)


def _events_from_array(path: Path, text: str) -> list[Event]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TaskgateError(f"{path}: invalid JSON ({exc.msg})") from exc
    if not isinstance(payload, list):
        raise TaskgateError(f"{path}: expected a JSON array of events")
    events: list[Event] = []
    for index, item in enumerate(payload, start=1):
        events.append(_validate(path, index, item))
    if not events:
        raise TaskgateError(f"{path}: trace is empty")
    return events


def _events_from_jsonl(path: Path, text: str) -> list[Event]:
    events: list[Event] = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TaskgateError(
                f"{path}:{line_number}: invalid JSON ({exc.msg})"
            ) from exc
        events.append(_validate(path, line_number, item))
    if not events:
        raise TaskgateError(f"{path}: trace is empty")
    return events


def _validate(path: Path, line: int, item: Any) -> Event:
    where = f"{path}:{line}"
    if not isinstance(item, dict):
        raise TaskgateError(f"{where}: event must be a JSON object")
    kind = item.get("type")
    if kind not in {"run", "model", "tool", "run_end"}:
        raise TaskgateError(f"{where}: unknown event type {kind!r}")
    data = dict(item)
    if kind == "tool":
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise TaskgateError(f"{where}: tool event needs a non-empty name")
        _optional_str(data, "idempotency_key", where)
        _optional_bool(data, "mutating", where)
        _optional_bool(data, "ok", where)
    elif kind == "run":
        _optional_str(data, "id", where)
        _optional_str(data, "task", where)
        _optional_str(data, "started_at", where)
    elif kind == "run_end":
        status = data.get("status", "ok")
        if not isinstance(status, str) or not status:
            raise TaskgateError(f"{where}: run_end status must be a non-empty string")
        data["status"] = status
    for field in _INT_FIELDS:
        if field in data and data[field] is not None:
            value = data[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TaskgateError(f"{where}: {field} must be an integer >= 0")
    if "cost_usd" in data and data["cost_usd"] is not None:
        cost = data["cost_usd"]
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
            raise TaskgateError(f"{where}: cost_usd must be a number >= 0")
    return Event(kind=kind, data=data, line=line)


def _optional_str(data: dict[str, Any], field: str, where: str) -> None:
    if field in data and data[field] is not None and (
        not isinstance(data[field], str) or not data[field]
    ):
        raise TaskgateError(f"{where}: {field} must be a non-empty string")


def _optional_bool(data: dict[str, Any], field: str, where: str) -> None:
    if field in data and data[field] is not None and not isinstance(data[field], bool):
        raise TaskgateError(f"{where}: {field} must be a boolean")


class Recorder:
    """Write one taskgate JSONL trace from an agent loop."""

    def __init__(self, path: str | Path, task: str, run_id: str | None = None) -> None:
        self.path = Path(path)
        self.task = task
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._handle: TextIO | None = None

    def __enter__(self) -> Recorder:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8")
        self.event(
            {
                "type": "run",
                "id": self.run_id,
                "task": self.task,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        status = "error" if exc_type else "ok"
        self.event({"type": "run_end", "status": status})
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def model(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        cost_usd: float | None = None,
        latency_ms: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "type": "model",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cached_input_tokens,
        }
        if cost_usd is not None:
            payload["cost_usd"] = cost_usd
        if latency_ms is not None:
            payload["latency_ms"] = latency_ms
        self.event(payload)

    def tool(
        self,
        name: str,
        args: Any = None,
        *,
        mutating: bool = False,
        idempotency_key: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        cost_usd: float | None = None,
        latency_ms: int | None = None,
        ok: bool = True,
    ) -> None:
        payload: dict[str, Any] = {
            "type": "tool",
            "name": name,
            "mutating": mutating,
            "ok": ok,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cached_input_tokens,
        }
        if args is not None:
            payload["args"] = args
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        if cost_usd is not None:
            payload["cost_usd"] = cost_usd
        if latency_ms is not None:
            payload["latency_ms"] = latency_ms
        self.event(payload)

    def event(self, payload: dict[str, Any]) -> None:
        if self._handle is None:
            raise TaskgateError("Recorder is not open")
        self._handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self._handle.flush()
