"""Run telemetry: step timing, status, retries, and a persisted run record."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, Field

log = logging.getLogger("agent_ops")
R = TypeVar("R")


class StepRecord(BaseModel):
    name: str
    status: str = "running"  # running | ok | failed
    started_at: datetime
    duration_ms: float | None = None
    attempts: int = 1
    detail: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class RunRecord(BaseModel):
    run_id: str
    pipeline: str
    provider: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str = "running"
    steps: list[StepRecord] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    error: str | None = None


class RunRecorder:
    def __init__(self, pipeline: str, provider: str, runs_dir: Path):
        now = datetime.now(UTC)
        run_id = now.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.record = RunRecord(run_id=run_id, pipeline=pipeline, provider=provider, started_at=now)
        self.dir = runs_dir / run_id
        self.dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def step(self, name: str) -> Iterator[StepRecord]:
        rec = StepRecord(name=name, started_at=datetime.now(UTC))
        self.record.steps.append(rec)
        t0 = time.perf_counter()
        try:
            yield rec
            rec.status = "ok"
        except Exception as exc:
            rec.status, rec.error = "failed", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            rec.duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            log.info("step %s %s in %.1fms", name, rec.status, rec.duration_ms)

    def finish(self, status: str, usage: dict, error: str | None = None) -> Path:
        self.record.status, self.record.usage, self.record.error = status, usage, error
        self.record.finished_at = datetime.now(UTC)
        path = self.dir / "run.json"
        path.write_text(self.record.model_dump_json(indent=2), encoding="utf-8")
        return path


def retry(fn: Callable[[], R], *, attempts: int = 3, base_delay: float = 0.5,
          retry_on: tuple[type[BaseException], ...] = (Exception,), step: StepRecord | None = None,
          sleep: Callable[[float], None] = time.sleep) -> R:
    """Call ``fn`` with exponential backoff (0.5s, 1s, 2s, ...). Records attempts on ``step``."""
    for attempt in range(1, attempts + 1):
        try:
            if step is not None:
                step.attempts = attempt
            return fn()
        except retry_on as exc:
            if attempt == attempts:
                raise
            delay = base_delay * 2 ** (attempt - 1)
            log.warning("attempt %d/%d failed (%s), retrying in %.1fs", attempt, attempts, exc, delay)
            sleep(delay)
    raise AssertionError("unreachable")


def list_runs(runs_dir: Path) -> list[RunRecord]:
    records = []
    for path in sorted(runs_dir.glob("*/run.json"), reverse=True):
        try:
            records.append(RunRecord.model_validate(json.loads(path.read_text(encoding="utf-8"))))
        except (ValueError, OSError):
            continue
    return records
