"""Record a checkout trace the same way an agent loop would."""

from pathlib import Path

from taskgate import Recorder

OUT = Path(__file__).with_name("recorded-checkout.jsonl")


def main() -> None:
    if OUT.exists():
        OUT.unlink()
    with Recorder(OUT, task="checkout", run_id="recorded") as rec:
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
    print(OUT)


if __name__ == "__main__":
    main()
