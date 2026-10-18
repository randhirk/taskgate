"""taskgate.toml policy."""

from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 and older
    import tomli as tomllib

from dataclasses import dataclass
from pathlib import Path

from taskgate.trace import TaskgateError


@dataclass(frozen=True)
class Pricing:
    input_per_million: float = 0.0
    output_per_million: float = 0.0
    cached_input_per_million: float = 0.0


@dataclass(frozen=True)
class Policy:
    max_cost_usd: float | None = None
    max_tool_calls: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_latency_ms: int | None = None
    baseline: Path | None = None
    max_cost_increase_ratio: float | None = None
    max_tool_call_increase: int | None = None
    max_mutation_increase: int | None = None
    fail_on_duplicate: bool = True
    fail_on_error_status: bool = True
    mutation_names: tuple[str, ...] = ()
    pricing: Pricing | None = None

    def with_baseline(self, baseline: Path | None) -> Policy:
        return Policy(
            max_cost_usd=self.max_cost_usd,
            max_tool_calls=self.max_tool_calls,
            max_input_tokens=self.max_input_tokens,
            max_output_tokens=self.max_output_tokens,
            max_latency_ms=self.max_latency_ms,
            baseline=baseline,
            max_cost_increase_ratio=self.max_cost_increase_ratio,
            max_tool_call_increase=self.max_tool_call_increase,
            max_mutation_increase=self.max_mutation_increase,
            fail_on_duplicate=self.fail_on_duplicate,
            fail_on_error_status=self.fail_on_error_status,
            mutation_names=self.mutation_names,
            pricing=self.pricing,
        )


_SECTIONS = {
    "budget": {
        "max_cost_usd": "number",
        "max_tool_calls": "int",
        "max_input_tokens": "int",
        "max_output_tokens": "int",
        "max_latency_ms": "int",
    },
    "regression": {
        "baseline": "string",
        "max_cost_increase_ratio": "number",
        "max_tool_call_increase": "int",
        "max_mutation_increase": "int",
    },
    "mutations": {
        "fail_on_duplicate": "bool",
        "names": "string-list",
    },
    "run": {
        "fail_on_error_status": "bool",
    },
    "pricing": {
        "input_per_million": "number",
        "output_per_million": "number",
        "cached_input_per_million": "number",
    },
}


def load_policy(path: Path) -> Policy:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TaskgateError(f"{path}: {exc.strerror}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise TaskgateError(f"{path}: invalid TOML ({exc})") from exc
    if not isinstance(raw, dict):
        raise TaskgateError(f"{path}: policy must be a TOML table")

    for section in raw:
        if section not in _SECTIONS:
            raise TaskgateError(f"{path}: unknown section [{section}]")
        table = raw[section]
        if not isinstance(table, dict):
            raise TaskgateError(f"{path}: [{section}] must be a table")
        for key, value in table.items():
            if key not in _SECTIONS[section]:
                raise TaskgateError(f"{path}: unknown key {section}.{key}")
            _check_type(path, f"{section}.{key}", value, _SECTIONS[section][key])

    budget = raw.get("budget", {})
    regression = raw.get("regression", {})
    mutations = raw.get("mutations", {})
    run = raw.get("run", {})
    pricing_raw = raw.get("pricing")

    baseline: Path | None = None
    if "baseline" in regression:
        baseline = (path.parent / regression["baseline"]).resolve()

    pricing = None
    if pricing_raw is not None:
        pricing = Pricing(
            input_per_million=float(pricing_raw.get("input_per_million", 0)),
            output_per_million=float(pricing_raw.get("output_per_million", 0)),
            cached_input_per_million=float(pricing_raw.get("cached_input_per_million", 0)),
        )

    return Policy(
        max_cost_usd=_optional_float(budget.get("max_cost_usd")),
        max_tool_calls=budget.get("max_tool_calls"),
        max_input_tokens=budget.get("max_input_tokens"),
        max_output_tokens=budget.get("max_output_tokens"),
        max_latency_ms=budget.get("max_latency_ms"),
        baseline=baseline,
        max_cost_increase_ratio=_optional_float(regression.get("max_cost_increase_ratio")),
        max_tool_call_increase=regression.get("max_tool_call_increase"),
        max_mutation_increase=regression.get("max_mutation_increase"),
        fail_on_duplicate=mutations.get("fail_on_duplicate", True),
        fail_on_error_status=run.get("fail_on_error_status", True),
        mutation_names=tuple(mutations.get("names", ())),
        pricing=pricing,
    )


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)  # type: ignore[arg-type]


def _check_type(path: Path, name: str, value: object, kind: str) -> None:
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise TaskgateError(f"{path}: {name} must be a number >= 0")
    elif kind == "int":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TaskgateError(f"{path}: {name} must be an integer >= 0")
    elif kind == "string":
        if not isinstance(value, str) or not value:
            raise TaskgateError(f"{path}: {name} must be a non-empty string")
    elif kind == "bool":
        if not isinstance(value, bool):
            raise TaskgateError(f"{path}: {name} must be true or false")
    elif kind == "string-list":
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise TaskgateError(f"{path}: {name} must be an array of non-empty strings")


INIT_TEMPLATE = """\
# Fail the build when an agent task gets more expensive, busier, or repeats a mutation.
# Paths are relative to this file.

[budget]
max_cost_usd = 0.50
max_tool_calls = 25
max_input_tokens = 100000
max_output_tokens = 20000

[regression]
baseline = "baselines/task.jsonl"
max_cost_increase_ratio = 0.10
max_tool_call_increase = 2
max_mutation_increase = 0

[mutations]
fail_on_duplicate = true
# Tools matching these names are mutations even if the trace says otherwise.
names = ["charge_*", "create_*", "delete_*", "send_*"]

[run]
fail_on_error_status = true

# Used when an event has tokens and no cost_usd.
# input_tokens is the uncached prompt. cached_input_tokens is priced on its own.
[pricing]
input_per_million = 3.0
output_per_million = 15.0
cached_input_per_million = 0.30
"""
