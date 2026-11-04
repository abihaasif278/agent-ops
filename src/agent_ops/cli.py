"""agent-ops CLI: run | runs | show | schedule."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from .config import load_config
from .pipeline import Pipeline
from .telemetry import list_runs

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "pipeline.toml"


def _print_run(console: Console, record) -> None:
    table = Table(title=f"Run {record.run_id} - {record.status}")
    for col in ("Step", "Status", "ms", "Attempts", "Detail"):
        table.add_column(col)
    for s in record.steps:
        style = "green" if s.status == "ok" else "red"
        table.add_row(s.name, f"[{style}]{s.status}[/]", f"{s.duration_ms}", str(s.attempts),
                      json.dumps(s.detail) if s.detail else (s.error or ""))
    console.print(table)
    u = record.usage
    if u:
        console.print(f"LLM ({record.provider}): {u['calls']} calls, {u['input_tokens']} in / "
                      f"{u['output_tokens']} out tokens, est. cost ${u['cost_usd']:.4f} at {u['model']} pricing")
    if record.error:
        console.print(f"[red]{record.error}[/]")


def main() -> None:
    p = argparse.ArgumentParser(prog="agent-ops")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="Run the pipeline once")
    run.add_argument("--print", action="store_true", help="Print the brief to the terminal")
    run.add_argument("--json", action="store_true", help="Print the run record as JSON (for n8n/automation)")
    sub.add_parser("runs", help="List previous runs")
    show = sub.add_parser("show", help="Show one run and its brief")
    show.add_argument("run_id")
    sub.add_parser("schedule", help="Run on the configured cron schedule (blocking)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    console = Console()
    config = load_config(args.config)

    if args.cmd == "run":
        result = Pipeline(config).run()
        if args.json:
            print(result.record.model_dump_json(indent=2))
        else:
            _print_run(console, result.record)
            if result.markdown_path:
                console.print(f"Brief: {result.markdown_path}")
            if args.print and result.markdown:
                console.print(Markdown(result.markdown))
        sys.exit(0 if result.record.status == "ok" else 1)

    elif args.cmd == "runs":
        table = Table(title=f"Runs in {config.runs_dir}")
        for col in ("Run ID", "Status", "Provider", "Items", "Tokens", "Cost $"):
            table.add_column(col)
        for r in list_runs(config.runs_dir):
            analyse = next((s for s in r.steps if s.name == "analyse"), None)
            kept = analyse.detail.get("kept", "-") if analyse else "-"
            tokens = r.usage.get("input_tokens", 0) + r.usage.get("output_tokens", 0)
            table.add_row(r.run_id, r.status, r.provider, str(kept), str(tokens), f"{r.usage.get('cost_usd', 0):.4f}")
        console.print(table)

    elif args.cmd == "show":
        match = [r for r in list_runs(config.runs_dir) if r.run_id.startswith(args.run_id)]
        if not match:
            console.print(f"[red]No run matching {args.run_id}[/]")
            sys.exit(1)
        _print_run(console, match[0])
        md = match[0].outputs.get("markdown")
        if md and Path(md).exists():
            console.print(Markdown(Path(md).read_text(encoding="utf-8")))

    elif args.cmd == "schedule":
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger

        scheduler = BlockingScheduler(timezone=config.timezone)
        trigger = CronTrigger.from_crontab(config.schedule.lower(), timezone=config.timezone)
        scheduler.add_job(lambda: _print_run(console, Pipeline(config).run().record), trigger,
                          id="brief", max_instances=1, coalesce=True)
        next_run = trigger.get_next_fire_time(None, datetime.now(trigger.timezone))
        console.print(f"Scheduled '{config.name}' with cron '{config.schedule}' ({config.timezone}). "
                      f"Next run: {next_run}")
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            console.print("Scheduler stopped.")


if __name__ == "__main__":
    main()
