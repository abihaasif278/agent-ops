import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from agent_ops.agents import ResearcherAgent
from agent_ops.config import Feed, load_config
from agent_ops.llm import MockProvider, UsageMeter
from agent_ops.mock_rules import HANDLERS
from agent_ops.models import FeedItem
from agent_ops.pipeline import Pipeline
from agent_ops.telemetry import list_runs, retry

ROOT = Path(__file__).parents[1]


@pytest.fixture
def config(tmp_path):
    cfg = load_config(ROOT / "config" / "pipeline.toml")
    cfg.output.markdown_dir = str(tmp_path / "runs")
    return cfg


def test_full_run_produces_brief_and_record(config, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    result = Pipeline(config, llm=MockProvider(HANDLERS)).run(as_of=datetime(2026, 11, 2, tzinfo=UTC))
    rec = result.record
    assert rec.status == "ok", rec.error
    assert [s.name for s in rec.steps] == ["research", "analyse", "write", "deliver"]
    assert rec.steps[0].detail["fetched"] == 14
    assert rec.steps[1].detail["per_topic"] == {"AI agents": 2, "Workflow automation": 3, "LLM engineering": 3}

    md = result.markdown
    assert md.startswith("# Weekly AI Automation Brief")
    assert "## Key takeaways" in md and "## LLM engineering" in md
    assert "Company picnic" not in md and "Smartphone sales" not in md  # irrelevant items dropped

    assert rec.usage["calls"] == 14 + 8 + 1  # score all, summarise keepers, write once
    assert rec.usage["cost_usd"] > 0
    saved = json.loads((result.markdown_path.parent / "run.json").read_text())
    assert saved["run_id"] == rec.run_id
    assert list_runs(config.runs_dir)[0].run_id == rec.run_id


def test_broken_feed_does_not_fail_run(config):
    config.feeds.append(Feed(name="Missing", url="../data/feeds/does_not_exist.xml"))
    rec = Pipeline(config, llm=MockProvider(HANDLERS), sleep=lambda s: None).run().record
    assert rec.status == "ok"
    research = rec.steps[0]
    assert research.detail["failed_feeds"] == ["Missing"]
    assert research.attempts == 3  # retried before giving up


def test_all_feeds_failing_marks_run_failed(config):
    config.feeds = [Feed(name="Missing", url="nope.xml")]
    rec = Pipeline(config, llm=MockProvider(HANDLERS), sleep=lambda s: None).run().record
    assert rec.status == "failed" and "No items" in rec.error
    assert rec.steps[0].status == "failed"


def test_slack_delivery_posts_brief(config, monkeypatch):
    sent = {}

    def fake_post(url, json, timeout):
        sent["url"], sent["text"] = url, json["text"]
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/abc")
    monkeypatch.setattr(httpx, "post", fake_post)
    rec = Pipeline(config, llm=MockProvider(HANDLERS)).run().record
    assert rec.outputs["slack"] == "sent"
    assert sent["text"].startswith("# Weekly AI Automation Brief")


def test_http_feed_is_fetched(config):
    xml = (ROOT / "data" / "feeds" / "tech_wire.xml").read_bytes()
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=xml))
    researcher = ResearcherAgent(config, http=httpx.Client(transport=transport))
    items = researcher.fetch(Feed(name="Remote", url="https://example.test/rss"))
    assert len(items) == 4 and items[0].feed == "Remote"


def test_dedupe_and_lookback(config):
    mk = lambda t, d: FeedItem(id=t, feed="f", title=t, link="", summary="",  # noqa: E731
                               published=datetime(2026, 10, d, tzinfo=UTC))
    items = [mk("Hello World!", 30), mk("hello world", 29), mk("Old news", 1)]
    researcher = ResearcherAgent(config)
    assert [i.title for i in researcher.dedupe(items)] == ["Hello World!", "Old news"]
    config.lookback_days = 7
    assert [i.title for i in researcher.filter_recent(items, as_of=datetime(2026, 11, 2, tzinfo=UTC))] == [
        "Hello World!", "hello world"]


def test_retry_backoff_and_give_up():
    calls, delays = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("boom")
        return "ok"

    assert retry(flaky, attempts=3, base_delay=0.5, sleep=delays.append) == "ok"
    assert delays == [0.5, 1.0]
    with pytest.raises(ConnectionError):
        retry(lambda: (_ for _ in ()).throw(ConnectionError("x")), attempts=2, sleep=lambda s: None)


def test_usage_meter_cost():
    m = UsageMeter(model="claude-opus-5-5")
    m.add("t", 1_000_000, 100_000)
    assert m.cost_usd == pytest.approx(4.0 + 2.0)
    assert m.by_task["t"]["calls"] == 1


def test_cron_expression_is_valid(config):
    from apscheduler.triggers.cron import CronTrigger

    trigger = CronTrigger.from_crontab(config.schedule.lower(), timezone=config.timezone)
    nxt = trigger.get_next_fire_time(None, datetime(2026, 11, 4, 12, tzinfo=UTC))
    assert (nxt.weekday(), nxt.hour) == (0, 8)  # next Monday 08:00


def test_n8n_workflow_is_wired():
    wf = json.loads((ROOT / "n8n" / "weekly-brief.workflow.json").read_text())
    names = {n["name"] for n in wf["nodes"]}
    targets = {e["node"] for out in wf["connections"].values() for br in out["main"] for e in br}
    assert set(wf["connections"]) <= names and targets <= names
    assert names - targets == {"Every Monday 08:00"}  # only the trigger has no inbound edge
    cmd = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.executeCommand")
    assert "run --json" in cmd["parameters"]["command"]
