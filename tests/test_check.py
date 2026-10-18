from decimal import Decimal
from pathlib import Path

from taskgate.check import check_events
from taskgate.policy import Policy, Pricing, load_policy
from taskgate.trace import load_trace

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def test_checkout_matches_its_baseline():
    policy = load_policy(EXAMPLES / "taskgate.toml")
    report = check_events(
        load_trace(EXAMPLES / "checkout.jsonl"),
        policy,
        load_trace(policy.baseline),
    )
    assert report.ok
    assert report.task == "checkout"
    assert report.metrics.cost_usd == Decimal("0.020")
    assert report.metrics.tool_calls == 2
    assert report.metrics.unique_mutations == 1
    assert report.findings == ()


def test_duplicate_charge_fails_even_when_the_second_call_looks_successful():
    report = check_events(load_trace(EXAMPLES / "checkout-duplicate.jsonl"), Policy())
    assert not report.ok
    assert [finding.code for finding in report.findings] == ["duplicate-mutation"]
    assert report.metrics.unique_mutations == 2


def test_same_idempotency_key_is_one_mutation():
    report = check_events(load_trace(EXAMPLES / "checkout-retry.jsonl"), Policy())
    assert report.ok
    assert report.metrics.tool_calls == 3
    assert report.metrics.unique_mutations == 1


def test_regression_catches_cost_and_extra_tool_calls():
    policy = load_policy(EXAMPLES / "taskgate.toml")
    report = check_events(
        load_trace(EXAMPLES / "checkout-regression.jsonl"),
        policy,
        load_trace(policy.baseline),
    )
    assert [finding.code for finding in report.findings] == [
        "regression-cost",
        "regression-tool-calls",
    ]
    assert "130" in report.findings[0].message


def test_increase_equal_to_the_limit_passes():
    baseline = load_trace(EXAMPLES / "checkout.jsonl")
    # Model 0.014 plus the two 0.004 tool calls is 0.022, exactly 10% over 0.020.
    candidate = _with_model_cost(baseline, "0.014")
    policy = Policy(max_cost_increase_ratio=0.10, max_tool_call_increase=0)
    report = check_events(candidate, policy, baseline)
    assert report.ok


def test_reused_idempotency_key_with_different_args_fails():
    from taskgate.trace import Event

    events = [
        Event("tool", {"type": "tool", "name": "charge_card", "args": {"amount": 20}, "mutating": True, "idempotency_key": "pay_1"}, 1),
        Event("tool", {"type": "tool", "name": "charge_card", "args": {"amount": 30}, "mutating": True, "idempotency_key": "pay_1"}, 2),
    ]
    report = check_events(events, Policy())
    assert [finding.code for finding in report.findings] == ["idempotency-key-reused"]


def test_name_pattern_marks_a_tool_as_mutating():
    from taskgate.trace import Event

    events = [
        Event("tool", {"type": "tool", "name": "delete_file", "args": {"path": "a"}, "mutating": False}, 1),
        Event("tool", {"type": "tool", "name": "delete_file", "args": {"path": "a"}, "mutating": False}, 2),
    ]
    report = check_events(events, Policy(mutation_names=("delete_*",)))
    assert report.findings[0].code == "duplicate-mutation"


def test_pricing_bills_cached_tokens_separately(tmp_path: Path):
    trace = tmp_path / "priced.jsonl"
    trace.write_text(
        '{"type":"model","input_tokens":800,"cached_input_tokens":400,"output_tokens":60}\n',
        encoding="utf-8",
    )
    policy = Policy(
        pricing=Pricing(
            input_per_million=3,
            output_per_million=15,
            cached_input_per_million=0.3,
        )
    )
    report = check_events(load_trace(trace), policy)
    assert report.metrics.cost_usd == Decimal("0.00342")
    assert report.ok


def test_explicit_cost_wins_over_the_price_table(tmp_path: Path):
    trace = tmp_path / "explicit.jsonl"
    trace.write_text(
        '{"type":"model","input_tokens":800,"output_tokens":60,"cost_usd":0.01}\n',
        encoding="utf-8",
    )
    policy = Policy(pricing=Pricing(input_per_million=3, output_per_million=15))
    report = check_events(load_trace(trace), policy)
    assert report.metrics.cost_usd == Decimal("0.01")


def test_unpriced_tokens_fail_only_when_a_cost_gate_is_set(tmp_path: Path):
    trace = tmp_path / "tokens.jsonl"
    trace.write_text('{"type":"model","input_tokens":10,"output_tokens":2}\n', encoding="utf-8")
    events = load_trace(trace)
    warning = check_events(events, Policy())
    assert warning.ok
    assert warning.findings[0].code == "unpriced-usage"
    assert warning.findings[0].severity == "warning"

    failed = check_events(events, Policy(max_cost_usd=1))
    assert not failed.ok
    assert failed.findings[0].severity == "error"


def test_failed_run_status(tmp_path: Path):
    trace = tmp_path / "err.jsonl"
    trace.write_text('{"type":"run_end","status":"error"}\n', encoding="utf-8")
    report = check_events(load_trace(trace), Policy())
    assert report.findings[0].code == "run-failed"


def test_absolute_budgets(tmp_path: Path):
    trace = tmp_path / "busy.jsonl"
    trace.write_text(
        "\n".join(
            [
                '{"type":"model","cost_usd":0.2,"input_tokens":50,"output_tokens":10,"latency_ms":900}',
                '{"type":"tool","name":"search","mutating":false,"cost_usd":0.1,"latency_ms":200}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    report = check_events(
        load_trace(trace),
        Policy(max_cost_usd=0.25, max_tool_calls=0, max_latency_ms=1000, max_input_tokens=40),
    )
    assert [finding.code for finding in report.findings] == [
        "budget-cost",
        "budget-tool-calls",
        "budget-input-tokens",
        "budget-latency",
    ]


def _with_model_cost(events, cost: str):
    from taskgate.trace import Event

    updated = []
    for event in events:
        data = dict(event.data)
        if event.kind == "model":
            data["cost_usd"] = float(cost)
        updated.append(Event(event.kind, data, event.line))
    return updated
