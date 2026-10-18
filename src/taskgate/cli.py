"""Command line for taskgate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from taskgate.check import Report, check_events, render_text
from taskgate.policy import INIT_TEMPLATE, Policy, load_policy
from taskgate.trace import TaskgateError, load_trace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="taskgate",
        description=(
            "Fail CI when an AI agent task costs more, calls more tools, "
            "or repeats a mutation."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="check a trace against a policy")
    check.add_argument("trace", type=Path, help="JSONL file, JSON array, or directory of .jsonl files")
    check.add_argument("--policy", type=Path, help="taskgate.toml (default: ./taskgate.toml if present)")
    check.add_argument("--baseline", type=Path, help="baseline trace; overrides policy regression.baseline")
    check.add_argument("--format", choices=("text", "json"), default="text")
    check.add_argument("--quiet", action="store_true", help="print nothing; use the exit code")

    summary = sub.add_parser("summary", help="print metrics for a trace")
    summary.add_argument("trace", type=Path)
    summary.add_argument("--policy", type=Path, help="read pricing from this policy")
    summary.add_argument("--format", choices=("text", "json"), default="text")

    init = sub.add_parser("init", help="write a starter taskgate.toml")
    init.add_argument("--path", type=Path, default=Path("taskgate.toml"))

    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            return _cmd_check(args)
        if args.command == "summary":
            return _cmd_summary(args)
        return _cmd_init(args)
    except TaskgateError as exc:
        print(f"taskgate: {exc}", file=sys.stderr)
        return 2


def _cmd_check(args: argparse.Namespace) -> int:
    policy = _resolve_policy(args.policy, require=False)
    if args.baseline is not None:
        policy = policy.with_baseline(args.baseline.resolve())
    reports = _reports_for(args.trace, policy)
    if not args.quiet:
        _emit(reports, args.format)
    return 0 if all(report.ok for report in reports) else 1


def _cmd_summary(args: argparse.Namespace) -> int:
    pricing = load_policy(args.policy).pricing if args.policy else None
    events = load_trace(args.trace)
    report = check_events(
        events,
        Policy(pricing=pricing, fail_on_duplicate=False, fail_on_error_status=False),
    )
    if args.format == "json":
        print(json.dumps(report.metrics.to_dict(), indent=2))
    else:
        body = [
            line
            for line in render_text(report).splitlines()[1:]
            if not line.startswith("  error") and not line.startswith("  warning")
        ]
        print("\n".join(body))
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    path: Path = args.path
    if path.exists():
        raise TaskgateError(f"{path} already exists")
    path.write_text(INIT_TEMPLATE, encoding="utf-8")
    print(f"wrote {path}")
    return 0


def _resolve_policy(path: Path | None, require: bool) -> Policy:
    if path is not None:
        return load_policy(path)
    default = Path("taskgate.toml")
    if default.is_file():
        return load_policy(default)
    if require:
        raise TaskgateError("no taskgate.toml; pass --policy or run taskgate init")
    return Policy()


def _reports_for(trace: Path, policy: Policy) -> list[Report]:
    paths = _trace_paths(trace)
    if policy.baseline is not None and len(paths) != 1:
        raise TaskgateError("a baseline applies to one trace file, not a directory")
    baseline_events = None
    if policy.baseline is not None:
        if not policy.baseline.is_file():
            raise TaskgateError(f"baseline not found: {policy.baseline}")
        baseline_events = load_trace(policy.baseline)
    return [check_events(load_trace(path), policy, baseline_events) for path in paths]


def _trace_paths(trace: Path) -> list[Path]:
    if trace.is_dir():
        paths = sorted(trace.glob("*.jsonl"))
        if not paths:
            raise TaskgateError(f"{trace}: no .jsonl files")
        return paths
    if not trace.is_file():
        raise TaskgateError(f"{trace}: no such file")
    return [trace]


def _emit(reports: list[Report], fmt: str) -> None:
    if fmt == "json":
        payload: object
        payload = reports[0].to_dict() if len(reports) == 1 else [report.to_dict() for report in reports]
        print(json.dumps(payload, indent=2))
        return
    print("\n\n".join(render_text(report) for report in reports))
