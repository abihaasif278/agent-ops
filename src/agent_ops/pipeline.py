"""Orchestrator: research -> analyse -> write -> deliver, with a persisted run record."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from . import mock_rules
from .agents import AnalystAgent, ResearcherAgent, WriterAgent
from .config import PipelineConfig
from .llm import LLMProvider, get_provider
from .models import FeedItem, ItemSummary
from .telemetry import RunRecord, RunRecorder, retry

log = logging.getLogger("agent_ops")


@dataclass
class RunResult:
    record: RunRecord
    markdown_path: Path | None
    markdown: str | None


class Pipeline:
    def __init__(self, config: PipelineConfig, llm: LLMProvider | None = None,
                 researcher: ResearcherAgent | None = None, sleep=None):
        self.config = config
        self.llm = llm or get_provider(mock_rules.HANDLERS)
        self.researcher = researcher or ResearcherAgent(config)
        self.analyst = AnalystAgent(self.llm, config)
        self.writer = WriterAgent(self.llm, config)
        self._sleep = sleep  # injectable for tests

    def _retry(self, fn, step, **kw):
        if self._sleep is not None:
            kw["sleep"] = self._sleep
        return retry(fn, step=step, **kw)

    def run(self, as_of: datetime | None = None) -> RunResult:
        rec = RunRecorder(self.config.name, self.llm.name, self.config.runs_dir)
        try:
            # 1. Research: each feed is retried independently; one broken feed doesn't fail the run.
            items: list[FeedItem] = []
            with rec.step("research") as step:
                failed = []
                for feed in self.config.feeds:
                    try:
                        items += self._retry(lambda f=feed: self.researcher.fetch(f), step,
                                             retry_on=(httpx.HTTPError, OSError, ValueError))
                    except (httpx.HTTPError, OSError, ValueError) as exc:
                        log.error("feed %s failed: %s", feed.name, exc)
                        failed.append(feed.name)
                fetched = len(items)
                items = self.researcher.filter_recent(self.researcher.dedupe(items), as_of)
                step.detail = {"feeds": len(self.config.feeds), "failed_feeds": failed,
                               "fetched": fetched, "after_dedupe_and_date": len(items)}
                if not items:
                    raise RuntimeError("No items fetched from any feed")

            # 2. Analyse: score every item, keep the best per topic, summarise only the keepers.
            with rec.step("analyse") as step:
                scored = [(item, self.analyst.score(item)) for item in items]
                keep: dict[str, list[tuple[FeedItem, float]]] = {t.name: [] for t in self.config.topics}
                for item, rel in scored:
                    if rel.topic in keep and rel.score >= self.config.min_relevance:
                        keep[rel.topic].append((item, rel.score))
                sections: dict[str, list[tuple[FeedItem, ItemSummary]]] = {}
                for topic, pairs in keep.items():
                    pairs.sort(key=lambda p: (-p[1], -(p[0].published or datetime.min.replace(tzinfo=UTC)).timestamp()))
                    chosen = pairs[: self.config.max_items_per_topic]
                    if chosen:
                        sections[topic] = [(i, self.analyst.summarise(i, topic)) for i, _ in chosen]
                step.detail = {"scored": len(scored), "kept": sum(len(v) for v in sections.values()),
                               "dropped": len(scored) - sum(len(v) for v in sections.values()),
                               "per_topic": {t: len(v) for t, v in sections.items()}}

            # 3. Write
            with rec.step("write") as step:
                brief = self.writer.write(sections)
                markdown = self.writer.render(brief, sections, as_of or datetime.now(UTC))
                md_path = rec.dir / "brief.md"
                md_path.write_text(markdown, encoding="utf-8")
                rec.record.outputs["markdown"] = str(md_path)
                step.detail = {"chars": len(markdown), "takeaways": len(brief.takeaways)}

            # 4. Deliver (optional)
            with rec.step("deliver") as step:
                webhook = os.getenv(self.config.output.slack_webhook_env)
                if webhook:
                    self._retry(lambda: httpx.post(webhook, json={"text": markdown[:39000]},
                                                   timeout=15).raise_for_status(),
                                step, retry_on=(httpx.HTTPError,))
                    rec.record.outputs["slack"] = "sent"
                step.detail = {"slack": bool(webhook)}

            rec.finish("ok", self.llm.meter.snapshot())
            return RunResult(rec.record, md_path, markdown)
        except Exception as exc:
            rec.finish("failed", self.llm.meter.snapshot(), error=f"{type(exc).__name__}: {exc}")
            log.exception("run %s failed", rec.record.run_id)
            return RunResult(rec.record, None, None)
