"""Budget, regression, and duplicate-mutation checks."""

from __future__ import annotations

import fnmatch
import json
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from taskgate.policy import Policy, Pricing
from taskgate.trace import Event


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    severity: str = "error"
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }
        if self.line is not None:
            payload["line"] = self.line
        return payload


@dataclass(frozen=True)
class Metrics:
    cost_usd: Decimal
    tool_calls: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    latency_ms: int
    unique_mutations: int
    model_calls: int
    unpriced_token_events: int
    recorded_latency_events: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "cost_usd": float(self.cost_usd),
            "tool_calls": self.tool_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "latency_ms": self.latency_ms,
            "unique_mutations": self.unique_mutations,
            "model_calls": self.model_calls,
        }


@dataclass(frozen=True)
class Report:
    ok: bool
    task: str | None
    run_id: str | None
    metrics: Metrics
    baseline: Metrics | None
    findings: tuple[Finding, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "task": self.task,
            "run_id": self.run_id,
            "metrics": self.metrics.to_dict(),
            "baseline": None if self.baseline is None else self.baseline.to_dict(),
            "findings": [finding.to_dict() for finding in self.findings],
        }


def check_events(
    events: list[Event],
    policy: Policy,
    baseline_events: list[Event] | None = None,
) -> Report:
    metrics, mutations = _measure(events, policy)
    baseline = None if baseline_events is None else _measure(baseline_events, policy)[0]
    findings = _findings(events, policy, metrics, mutations, baseline)
    task, run_id = _identity(events)
    errors = [finding for finding in findings if finding.severity == "error"]
    return Report(
        ok=not errors,
        task=task,
        run_id=run_id,
        metrics=metrics,
        baseline=baseline,
        findings=tuple(findings),
    )


def render_text(report: Report) -> str:
    lines = [f"taskgate: {'PASS' if report.ok else 'FAIL'}"]
    if report.task:
        lines.append(f"  task        {report.task}")
    if report.run_id:
        lines.append(f"  run         {report.run_id}")
    lines.append(f"  cost        {_usd(report.metrics.cost_usd)}{_baseline_cost(report)}")
    lines.append(f"  tool calls  {report.metrics.tool_calls}{_baseline_tools(report)}")
    lines.append(f"  mutations   {report.metrics.unique_mutations}{_baseline_mutations(report)}")
    lines.append(
        "  tokens      "
        f"in {report.metrics.input_tokens}, "
        f"cached {report.metrics.cached_input_tokens}, "
        f"out {report.metrics.output_tokens}"
    )
    lines.append(f"  latency     {report.metrics.latency_ms} ms")
    for finding in report.findings:
        where = f" line {finding.line}" if finding.line else ""
        lines.append(f"  {finding.severity} {finding.code}{where}: {finding.message}")
    return "\n".join(lines)


def _identity(events: list[Event]) -> tuple[str | None, str | None]:
    for event in events:
        if event.kind == "run":
            return event.data.get("task"), event.data.get("id")
    return None, None


def _measure(events: list[Event], policy: Policy) -> tuple[Metrics, list[_Call]]:
    cost = Decimal("0")
    tool_calls = 0
    input_tokens = 0
    output_tokens = 0
    cached_input_tokens = 0
    latency_ms = 0
    model_calls = 0
    unpriced = 0
    recorded_latency = 0
    calls: list[_Call] = []

    for event in events:
        if event.kind not in {"model", "tool"}:
            continue
        event_cost, priced = _event_cost(event.data, policy.pricing)
        cost += event_cost
        if not priced:
            unpriced += 1
        input_tokens += int(event.data.get("input_tokens") or 0)
        output_tokens += int(event.data.get("output_tokens") or 0)
        cached_input_tokens += int(event.data.get("cached_input_tokens") or 0)
        if event.data.get("latency_ms") is not None:
            latency_ms += int(event.data["latency_ms"])
            recorded_latency += 1
        if event.kind == "model":
            model_calls += 1
            continue
        tool_calls += 1
        if _is_mutating(event, policy.mutation_names):
            calls.append(
                _Call(
                    name=str(event.data["name"]),
                    args_key=_canonical_args(event.data.get("args")),
                    idempotency_key=event.data.get("idempotency_key"),
                    line=event.line,
                )
            )

    unique = _collapsed_effects(calls)
    return (
        Metrics(
            cost_usd=cost,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            latency_ms=latency_ms,
            unique_mutations=unique,
            model_calls=model_calls,
            unpriced_token_events=unpriced,
            recorded_latency_events=recorded_latency,
        ),
        calls,
    )


@dataclass(frozen=True)
class _Call:
    name: str
    args_key: str
    idempotency_key: str | None
    line: int


def _findings(
    events: list[Event],
    policy: Policy,
    metrics: Metrics,
    calls: list[_Call],
    baseline: Metrics | None,
) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(_mutation_findings(calls, policy.fail_on_duplicate))
    if policy.fail_on_error_status:
        for event in events:
            if event.kind == "run_end" and event.data.get("status") != "ok":
                findings.append(
                    Finding(
                        "run-failed",
                        f"run status is {event.data.get('status')!r}",
                        line=event.line,
                    )
                )

    _budget_number(
        findings,
        "budget-cost",
        "cost",
        metrics.cost_usd,
        None if policy.max_cost_usd is None else Decimal(str(policy.max_cost_usd)),
        _usd,
    )
    _budget_int(findings, "budget-tool-calls", "tool calls", metrics.tool_calls, policy.max_tool_calls)
    _budget_int(findings, "budget-input-tokens", "input tokens", metrics.input_tokens, policy.max_input_tokens)
    _budget_int(
        findings,
        "budget-output-tokens",
        "output tokens",
        metrics.output_tokens,
        policy.max_output_tokens,
    )
    if policy.max_latency_ms is not None and metrics.recorded_latency_events == 0 and (
        metrics.tool_calls or metrics.model_calls
    ):
        findings.append(
            Finding(
                "unmeasured-latency",
                f"latency budget is {policy.max_latency_ms} ms but the trace records no latency",
            )
        )
    elif policy.max_latency_ms is not None and metrics.latency_ms > policy.max_latency_ms:
        findings.append(
            Finding(
                "budget-latency",
                f"latency {_ms(metrics.latency_ms)} exceeds {_ms(policy.max_latency_ms)}",
            )
        )

    cost_gate = policy.max_cost_usd is not None or (
        baseline is not None and policy.max_cost_increase_ratio is not None
    )
    if metrics.unpriced_token_events:
        findings.append(
            Finding(
                "unpriced-usage",
                (
                    f"{metrics.unpriced_token_events} event(s) have tokens and no cost_usd, "
                    "and no pricing table is configured"
                ),
                severity="error" if cost_gate else "warning",
            )
        )

    if baseline is not None:
        findings.extend(_regression(metrics, baseline, policy))
    return findings


def _mutation_findings(calls: list[_Call], fail_on_duplicate: bool) -> list[Finding]:
    findings: list[Finding] = []
    seen_keys: dict[str, _Call] = {}
    for call in calls:
        if not call.idempotency_key:
            continue
        previous = seen_keys.get(call.idempotency_key)
        if previous is None:
            seen_keys[call.idempotency_key] = call
            continue
        if previous.name != call.name or previous.args_key != call.args_key:
            findings.append(
                Finding(
                    "idempotency-key-reused",
                    (
                        f"idempotency key {call.idempotency_key!r} was reused with different "
                        f"arguments for {call.name} (lines {previous.line} and {call.line})"
                    ),
                    line=call.line,
                )
            )

    groups: dict[tuple[str, str], list[_Call]] = defaultdict(list)
    for call in calls:
        groups[(call.name, call.args_key)].append(call)

    if not fail_on_duplicate:
        return findings

    for (name, _args_key), group in groups.items():
        effects = _effect_count(group)
        if effects <= 1:
            continue
        lines = ", ".join(str(call.line) for call in group)
        findings.append(
            Finding(
                "duplicate-mutation",
                (
                    f"{name} with the same arguments ran as {effects} separate side effects "
                    f"(lines {lines}). Retry the same idempotency key to count as one mutation."
                ),
                line=group[-1].line,
            )
        )
    return findings


def _effect_count(group: list[_Call]) -> int:
    keyed: set[str] = set()
    bare = 0
    for call in group:
        if call.idempotency_key:
            keyed.add(call.idempotency_key)
        else:
            bare += 1
    return len(keyed) + bare


def _collapsed_effects(calls: list[_Call]) -> int:
    groups: dict[tuple[str, str], list[_Call]] = defaultdict(list)
    for call in calls:
        groups[(call.name, call.args_key)].append(call)
    return sum(_effect_count(group) for group in groups.values())


def _regression(metrics: Metrics, baseline: Metrics, policy: Policy) -> list[Finding]:
    findings: list[Finding] = []
    if policy.max_cost_increase_ratio is not None:
        limit = Decimal(str(policy.max_cost_increase_ratio))
        if baseline.cost_usd == 0:
            if metrics.cost_usd > 0:
                findings.append(
                    Finding(
                        "regression-cost",
                        f"cost rose from {_usd(baseline.cost_usd)} to {_usd(metrics.cost_usd)}",
                    )
                )
        else:
            increase = (metrics.cost_usd - baseline.cost_usd) / baseline.cost_usd
            if increase > limit:
                percent = (increase * 100).quantize(Decimal("0.1"))
                cap = (limit * 100).quantize(Decimal("0.1"))
                findings.append(
                    Finding(
                        "regression-cost",
                        (
                            f"cost increased {percent}% from {_usd(baseline.cost_usd)} "
                            f"to {_usd(metrics.cost_usd)} (limit {cap}%)"
                        ),
                    )
                )
    if policy.max_tool_call_increase is not None:
        delta = metrics.tool_calls - baseline.tool_calls
        if delta > policy.max_tool_call_increase:
            findings.append(
                Finding(
                    "regression-tool-calls",
                    (
                        f"tool calls increased by {delta} "
                        f"from {baseline.tool_calls} to {metrics.tool_calls} "
                        f"(limit {policy.max_tool_call_increase})"
                    ),
                )
            )
    if policy.max_mutation_increase is not None:
        delta = metrics.unique_mutations - baseline.unique_mutations
        if delta > policy.max_mutation_increase:
            findings.append(
                Finding(
                    "regression-mutations",
                    (
                        f"mutations increased by {delta} "
                        f"from {baseline.unique_mutations} to {metrics.unique_mutations} "
                        f"(limit {policy.max_mutation_increase})"
                    ),
                )
            )
    return findings


def _budget_number(
    findings: list[Finding],
    code: str,
    label: str,
    actual: Decimal,
    limit: Decimal | None,
    fmt: Any,
) -> None:
    if limit is not None and actual > limit:
        findings.append(Finding(code, f"{label} {fmt(actual)} exceeds {fmt(limit)}"))


def _budget_int(
    findings: list[Finding],
    code: str,
    label: str,
    actual: int,
    limit: int | None,
) -> None:
    if limit is not None and actual > limit:
        findings.append(Finding(code, f"{label} {actual} exceeds {limit}"))


def _event_cost(data: dict[str, Any], pricing: Pricing | None) -> tuple[Decimal, bool]:
    if data.get("cost_usd") is not None:
        return Decimal(str(data["cost_usd"])), True
    has_tokens = any(int(data.get(field) or 0) > 0 for field in ("input_tokens", "output_tokens", "cached_input_tokens"))
    if not has_tokens:
        return Decimal("0"), True
    if pricing is None:
        return Decimal("0"), False
    cost = (
        Decimal(int(data.get("input_tokens") or 0)) * Decimal(str(pricing.input_per_million))
        + Decimal(int(data.get("cached_input_tokens") or 0)) * Decimal(str(pricing.cached_input_per_million))
        + Decimal(int(data.get("output_tokens") or 0)) * Decimal(str(pricing.output_per_million))
    ) / Decimal(1_000_000)
    return cost, True


def _is_mutating(event: Event, patterns: tuple[str, ...]) -> bool:
    name = str(event.data.get("name") or "")
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns):
        return True
    return bool(event.data.get("mutating"))


def _canonical_args(args: Any) -> str:
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


def _usd(value: Decimal) -> str:
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    if text in {"", "-0"}:
        text = "0"
    return f"${text}"


def _ms(value: int) -> str:
    return f"{value} ms"


def _baseline_cost(report: Report) -> str:
    if report.baseline is None:
        return ""
    return f"  (baseline {_usd(report.baseline.cost_usd)})"


def _baseline_tools(report: Report) -> str:
    if report.baseline is None:
        return ""
    return f"  (baseline {report.baseline.tool_calls})"


def _baseline_mutations(report: Report) -> str:
    if report.baseline is None:
        return ""
    return f"  (baseline {report.baseline.unique_mutations})"
