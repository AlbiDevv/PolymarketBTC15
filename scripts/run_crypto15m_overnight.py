from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _run_step(name: str, command: list[str], log_dir: Path, *, allow_fail: bool = False) -> dict:
    started = datetime.now(timezone.utc)
    log_path = log_dir / f"{name}.log"
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(command)}\n\n")
        log.flush()
        proc = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="")
            log.write(line)
        code = proc.wait()
    finished = datetime.now(timezone.utc)
    result = {
        "name": name,
        "command": command,
        "exit_code": code,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_sec": (finished - started).total_seconds(),
        "log_path": str(log_path),
    }
    if code != 0 and not allow_fail:
        raise SystemExit(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run full local Crypto15m data/training/backtest pipeline")
    parser.add_argument("--config", default="config/settings.crypto15m.yaml")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--max-slug-requests", type=int, default=20000)
    parser.add_argument("--slug-concurrency", type=int, default=12)
    parser.add_argument("--price-max-markets", type=int, default=20000)
    parser.add_argument("--price-concurrency", type=int, default=8)
    parser.add_argument("--skip-ohlcv", action="store_true")
    args = parser.parse_args()

    run_id = _utc_stamp()
    out_dir = PROJECT_ROOT / "research" / "artifacts" / "crypto15m" / "overnight" / run_id
    log_dir = out_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    py = sys.executable
    steps: list[dict] = []

    if not args.skip_ohlcv:
        steps.append(_run_step("01_fetch_crypto_ohlcv", [
            py, "scripts/fetch_crypto_ohlcv.py", "--config", args.config,
        ], log_dir))

    steps.append(_run_step("02_sync_crypto15m_markets", [
        py,
        "scripts/sync_crypto15m_markets.py",
        "--config", args.config,
        "--max-pages", str(args.max_pages),
        "--slug-assets", "btc,eth",
        "--slug-timeframes", "15",
        "--max-slug-requests", str(args.max_slug_requests),
        "--slug-concurrency", str(args.slug_concurrency),
        "--slug-chunk-size", "50",
    ], log_dir))

    steps.append(_run_step("03_readiness_after_sync", [
        py, "scripts/crypto15m_readiness.py", "--config", args.config,
    ], log_dir))

    steps.append(_run_step("04_fetch_price_windows", [
        py,
        "scripts/fetch_crypto15m_price_windows.py",
        "--config", args.config,
        "--max-markets", str(args.price_max_markets),
        "--concurrency", str(args.price_concurrency),
        "--skip-existing",
    ], log_dir))

    steps.append(_run_step("05_build_dataset", [
        py, "scripts/build_crypto15m_dataset.py", "--config", args.config,
    ], log_dir))

    steps.append(_run_step("06_train_model", [
        py, "scripts/train_crypto15m_model.py", "--config", args.config,
    ], log_dir))

    steps.append(_run_step("07_readiness_after_train", [
        py, "scripts/crypto15m_readiness.py", "--config", args.config,
    ], log_dir))

    steps.append(_run_step("08_backtest", [
        py, "scripts/backtest_crypto15m.py", "--config", args.config,
    ], log_dir, allow_fail=True))

    verdict_path = PROJECT_ROOT / "research" / "artifacts" / "crypto15m" / "latest_verdict.json"
    verdict = _read_json(verdict_path)
    summary = {
        "run_id": run_id,
        "config": args.config,
        "steps": steps,
        "verdict": verdict,
        "accepted": bool(verdict.get("accepted")),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    report_path = out_dir / "REPORT.md"
    report_path.write_text(
        "\n".join([
            "# Crypto15m Overnight Training Report",
            "",
            f"- Run ID: `{run_id}`",
            f"- Accepted: `{bool(verdict.get('accepted'))}`",
            f"- Reason: `{verdict.get('reason', 'missing_verdict')}`",
            f"- Rows: `{verdict.get('rows', 0)}`",
            f"- Markets used: `{verdict.get('markets_used', 0)}`",
            f"- Coverage days: `{verdict.get('coverage_days', 0)}`",
            f"- Summary JSON: `{summary_path}`",
            "",
            "## Steps",
            *[
                f"- `{step['name']}` exit={step['exit_code']} duration={step['duration_sec']:.1f}s log=`{step['log_path']}`"
                for step in steps
            ],
            "",
            "## Verdict",
            "Backtest is valid only if the artifact is accepted. A rejected artifact means the model must not be promoted to live/shadow A/B.",
        ]),
        encoding="utf-8",
    )
    print(json.dumps({"summary": str(summary_path), "report": str(report_path), "accepted": bool(verdict.get("accepted")), "reason": verdict.get("reason")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
