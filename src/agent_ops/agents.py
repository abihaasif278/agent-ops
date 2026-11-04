"""The three agents in the pipeline.

ResearcherAgent  - pulls RSS/Atom feeds, normalises and de-duplicates items (no LLM)
AnalystAgent     - LLM: scores each item's relevance to the configured topics, summarises keepers
WriterAgent      - LLM: writes the brief's intro and takeaways, renders Markdown
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import feedparser
import httpx

from .config import Feed, PipelineConfig
from .llm import LLMProvider
from .models import Brief, FeedItem, ItemSummary, Relevance

TAG_RE = re.compile(r"<[^>]+>")


class ResearcherAgent:
    def __init__(self, config: PipelineConfig, http: httpx.Client | None = None):
        self.config = config
        self.http = http or httpx.Client(timeout=20, follow_redirects=True,
                                         headers={"User-Agent": "agent-ops/0.1 (+rss reader)"})

    def fetch(self, feed: Feed) -> list[FeedItem]:
        location = self.config.resolve(feed.url)
        if location.startswith("http"):
            resp = self.http.get(location)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        else:
            parsed = feedparser.parse(Path(location).read_bytes())
        if parsed.bozo and not parsed.entries:
            raise ValueError(f"Could not parse feed {feed.name}: {parsed.bozo_exception}")
        items = []
        for e in parsed.entries:
            published = None
            if getattr(e, "published_parsed", None):
                published = datetime(*e.published_parsed[:6], tzinfo=UTC)
            link = e.get("link", "")
            title = e.get("title", "").strip()
            items.append(FeedItem(
                id=hashlib.sha1((link or title).encode()).hexdigest()[:10], feed=feed.name, title=title,
                link=link, summary=TAG_RE.sub("", e.get("summary", "")).strip(), published=published,
            ))
        return items

    @staticmethod
    def dedupe(items: list[FeedItem]) -> list[FeedItem]:
        seen: set[str] = set()
        out = []
        for item in items:
            key = re.sub(r"[^a-z0-9]", "", item.title.lower())
            if key and key not in seen:
                seen.add(key)
                out.append(item)
        return out

    def filter_recent(self, items: list[FeedItem], as_of: datetime | None = None) -> list[FeedItem]:
        if not self.config.lookback_days:
            return items
        cutoff = (as_of or datetime.now(UTC)) - timedelta(days=self.config.lookback_days)
        return [i for i in items if i.published is None or i.published >= cutoff]


class AnalystAgent:
    SYSTEM_RELEVANCE = ("You are a research analyst for an AI automation consultancy. Decide whether a news item "
                        "is relevant to one of the given topics. Score 0-1. Be strict: general tech news is not "
                        "relevant unless it changes how teams build automations or AI features.")
    SYSTEM_SUMMARY = ("Summarise the item in 1-2 neutral sentences using only facts in the item, then one sentence "
                      "on why it matters to a team building AI automations. No hype words.")

    def __init__(self, llm: LLMProvider, config: PipelineConfig):
        self.llm, self.config = llm, config

    def score(self, item: FeedItem) -> Relevance:
        topics = "\n".join(f"- {t.name}: {', '.join(t.keywords)}" for t in self.config.topics)
        return self.llm.structured(
            task="score_relevance", system=self.SYSTEM_RELEVANCE, schema=Relevance,
            prompt=f"Topics:\n{topics}\n\nItem title: {item.title}\nItem text: {item.summary}",
            context={"item": item.model_dump(), "topics": [t.model_dump() for t in self.config.topics]},
        )

    def summarise(self, item: FeedItem, topic: str) -> ItemSummary:
        return self.llm.structured(
            task="summarise_item", system=self.SYSTEM_SUMMARY, schema=ItemSummary,
            prompt=f"Topic: {topic}\nTitle: {item.title}\nText: {item.summary}",
            context={"item": item.model_dump(), "topic": topic},
        )


class WriterAgent:
    SYSTEM = ("You write a concise weekly brief for an AI automation team. Write a 2-3 sentence intro naming the "
              "main themes, then 3 practical takeaways. Only reference items you are given.")

    def __init__(self, llm: LLMProvider, config: PipelineConfig):
        self.llm, self.config = llm, config

    def write(self, sections: dict[str, list[tuple[FeedItem, ItemSummary]]]) -> Brief:
        listing = "\n".join(f"[{topic}] {s.headline}: {s.summary}"
                            for topic, pairs in sections.items() for _, s in pairs)
        return self.llm.structured(
            task="write_brief", system=self.SYSTEM, schema=Brief,
            prompt=f"Brief title: {self.config.name}\n\nItems:\n{listing}",
            context={"title": self.config.name,
                     "sections": {t: [s.model_dump() for _, s in pairs] for t, pairs in sections.items()}},
        )

    @staticmethod
    def render(brief: Brief, sections: dict[str, list[tuple[FeedItem, ItemSummary]]], date: datetime) -> str:
        lines = [f"# {brief.title}", f"_{date:%A %d %B %Y}_", "", brief.intro, "", "## Key takeaways"]
        lines += [f"- {t}" for t in brief.takeaways]
        for topic, pairs in sections.items():
            lines += ["", f"## {topic}"]
            for item, s in pairs:
                lines += ["", f"### [{s.headline}]({item.link})", "", s.summary, "",
                          f"**Why it matters:** {s.why_it_matters}", "", f"_Source: {item.feed}_"]
        return "\n".join(lines) + "\n"
