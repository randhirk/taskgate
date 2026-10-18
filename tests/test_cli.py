from pathlib import Path

from taskgate.cli import main

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def test_check_examples(capsys):
    assert main(["check", str(EXAMPLES / "checkout.jsonl"), "--policy", str(EXAMPLES / "taskgate.toml")]) == 0
    out = capsys.readouterr().out
    assert out.startswith("taskgate: PASS")

    assert main(["check", str(EXAMPLES / "checkout-duplicate.jsonl")]) == 1
    assert "duplicate-mutation" in capsys.readouterr().out

    assert main(["check", str(EXAMPLES / "checkout-regression.jsonl"), "--policy", str(EXAMPLES / "taskgate.toml")]) == 1
    failed = capsys.readouterr().out
    assert "regression-cost" in failed
    assert "regression-tool-calls" in failed


def test_json_report(capsys):
    code = main(["check", str(EXAMPLES / "checkout-duplicate.jsonl"), "--format", "json"])
    assert code == 1
    assert '"ok": false' in capsys.readouterr().out


def test_quiet_still_sets_the_exit_code(capsys):
    assert main(["check", str(EXAMPLES / "checkout-duplicate.jsonl"), "--quiet"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_directory_without_a_baseline_checks_every_file():
    code = main(["check", str(EXAMPLES)])
    assert code == 1


def test_directory_rejects_a_baseline():
    code = main(["check", str(EXAMPLES), "--policy", str(EXAMPLES / "taskgate.toml")])
    assert code == 2


def test_missing_file_and_bad_policy(tmp_path: Path, capsys):
    assert main(["check", str(tmp_path / "missing.jsonl")]) == 2
    err = capsys.readouterr().err
    assert "no such file" in err.lower()

    policy = tmp_path / "taskgate.toml"
    policy.write_text("[budget]\nmax_cst = 1\n", encoding="utf-8")
    trace = tmp_path / "ok.jsonl"
    trace.write_text('{"type":"run","task":"t"}\n', encoding="utf-8")
    assert main(["check", str(trace), "--policy", str(policy)]) == 2
    assert "unknown key" in capsys.readouterr().err


def test_init_refuses_to_overwrite(tmp_path: Path, capsys):
    dest = tmp_path / "taskgate.toml"
    assert main(["init", "--path", str(dest)]) == 0
    assert dest.read_text(encoding="utf-8").startswith("# Fail the build")
    assert main(["init", "--path", str(dest)]) == 2
    assert "already exists" in capsys.readouterr().err


def test_summary_prints_cost(capsys):
    assert main(["summary", str(EXAMPLES / "checkout.jsonl")]) == 0
    assert "$0.02" in capsys.readouterr().out
