import json
from pathlib import Path

import pytest

from taskgate.trace import Recorder, TaskgateError, load_trace


def test_json_array_and_blank_lines(tmp_path: Path):
    array = tmp_path / "array.json"
    array.write_text(
        json.dumps(
            [
                {"type": "tool", "name": "search", "mutating": False, "cost_usd": 0.01},
            ]
        ),
        encoding="utf-8",
    )
    assert load_trace(array)[0].data["name"] == "search"

    jsonl = tmp_path / "lines.jsonl"
    jsonl.write_text('\n{"type":"run","task":"t"}\n\n', encoding="utf-8")
    assert load_trace(jsonl)[0].data["task"] == "t"


def test_invalid_events_name_the_line(tmp_path: Path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"type":"tool"}\n', encoding="utf-8")
    with pytest.raises(TaskgateError, match="non-empty name"):
        load_trace(path)

    path.write_text("{nope}\n", encoding="utf-8")
    with pytest.raises(TaskgateError, match=r":1: invalid JSON"):
        load_trace(path)

    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(TaskgateError, match="empty"):
        load_trace(empty)


def test_recorder_round_trip(tmp_path: Path):
    path = tmp_path / "run.jsonl"
    with Recorder(path, task="checkout", run_id="r1") as rec:
        rec.model(input_tokens=3, output_tokens=1, cost_usd=0.001)
        rec.tool("charge_card", {"amount": 5}, mutating=True, idempotency_key="k")
    events = load_trace(path)
    assert [event.kind for event in events] == ["run", "model", "tool", "run_end"]
    assert events[0].data["task"] == "checkout"
    assert events[-1].data["status"] == "ok"


def test_recorder_marks_a_raised_run_as_error(tmp_path: Path):
    path = tmp_path / "run.jsonl"
    with pytest.raises(RuntimeError):
        with Recorder(path, task="checkout") as rec:
            rec.tool("search", mutating=False)
            raise RuntimeError("boom")
    assert load_trace(path)[-1].data["status"] == "error"
