# taskgate

Fail CI when an AI agent task costs more, calls more tools, or repeats a mutation.

A finished agent reply can look fine while the run retried a charge, looped on search, or blew the token budget. taskgate checks the trace of the whole task. It is a single Python file format and a CLI with no dependencies. It does not replace a trace server or a prompt eval suite.

## Install

```bash
pip install -e .
taskgate check examples/checkout.jsonl --policy examples/taskgate.toml
```

Python 3.9 or newer. On 3.11 and newer the TOML parser comes from the standard library. On 3.9 and 3.10, pip installs `tomli`.

## What it catches

| Check | Fails when |
| --- | --- |
| `duplicate-mutation` | The same mutating tool is invoked again with the same arguments and a different idempotency key, or with no key |
| `idempotency-key-reused` | One idempotency key is reused with different arguments |
| `budget-*` | Cost, tool calls, tokens, or latency exceed an absolute cap |
| `regression-*` | Cost, tool calls, or mutation count grow past the baseline by more than the allowed delta |
| `run-failed` | The trace ends with a status other than `ok` |
| `unpriced-usage` | Events have tokens but no `cost_usd` and no price table. This is a warning, and an error when a cost gate is set |

A retry that sends the **same idempotency key** and the **same arguments** counts as one mutation. It still counts as another tool call and still adds its cost. That split is deliberate: a safe retry should not look like a second charge, and it should not be free.

`input_tokens` is the uncached prompt. Put cache hits in `cached_input_tokens`. If an event sets `cost_usd`, that value is used and the price table is ignored for that event.

## Record a trace

```python
from taskgate import Recorder

with Recorder("traces/checkout.jsonl", task="checkout") as rec:
    rec.model(input_tokens=800, output_tokens=60, cached_input_tokens=400, cost_usd=0.012)
    rec.tool("search_catalog", {"q": "lamp"}, cost_usd=0.004, latency_ms=80)
    rec.tool(
        "charge_card",
        {"amount": 20, "currency": "USD"},
        mutating=True,
        idempotency_key="pay_9",
        cost_usd=0.004,
        latency_ms=200,
    )
```

Each line is one JSON object. A JSON array of the same objects is also accepted. Mark a tool `mutating: true` when it can charge, create, delete, send, or otherwise change something outside the model. A timeout still counts: if the call might have committed, leave `mutating` true.

```json
{"type":"run","id":"demo","task":"checkout"}
{"type":"model","input_tokens":800,"output_tokens":60,"cached_input_tokens":400,"cost_usd":0.012}
{"type":"tool","name":"charge_card","args":{"amount":20,"currency":"USD"},"mutating":true,"idempotency_key":"pay_9","cost_usd":0.004,"latency_ms":200,"ok":true}
{"type":"run_end","status":"ok"}
```

## Policy

`taskgate init` writes a starter `taskgate.toml`. Paths inside it are relative to the policy file.

```toml
[budget]
max_cost_usd = 0.50
max_tool_calls = 25

[regression]
baseline = "baselines/checkout.jsonl"
max_cost_increase_ratio = 0.10
max_tool_call_increase = 2
max_mutation_increase = 0

[mutations]
fail_on_duplicate = true
names = ["charge_*", "create_*", "delete_*", "send_*"]

[pricing]
input_per_million = 3.0
output_per_million = 15.0
cached_input_per_million = 0.30
```

`names` uses shell-style wildcards. A matching tool is treated as mutating even if the event says otherwise. An increase equal to a limit passes. Anything past it fails.

Unknown keys are an error, so a typo in `max_cost_usd` cannot silently disable the gate.

## CI

```yaml
- name: Check the agent task
  run: taskgate check traces/checkout.jsonl --policy taskgate.toml
```

Exit `0` means the trace passed. Exit `1` means at least one finding. Exit `2` means the trace or the policy could not be read.

```bash
taskgate check examples/checkout-duplicate.jsonl
taskgate check examples/checkout-regression.jsonl --policy examples/taskgate.toml
taskgate check traces/ --format json
taskgate summary traces/checkout.jsonl
```

A baseline applies to one trace. Checking a directory uses the absolute budget and the mutation rules only.

## Library

```python
from taskgate import check_events, load_policy, load_trace

policy = load_policy("taskgate.toml")
report = check_events(
    load_trace("traces/checkout.jsonl"),
    policy,
    load_trace(policy.baseline) if policy.baseline else None,
)
assert report.ok, report.findings
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT.
