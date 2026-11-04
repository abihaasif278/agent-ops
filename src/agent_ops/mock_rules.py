"""Deterministic handlers for LLM_PROVIDER=mock: keyword relevance, extractive summaries, templated brief."""

from __future__ import annotations

import re
from typing import Any

from .models import Brief, ItemSummary, Relevance

IMPLICATIONS = {
    "AI agents": "Agent features are moving from demos to production patterns, so plan for tool permissions, "
                 "human handoff and cost limits from the start.",
    "Workflow automation": "Reliable automations depend on retries, idempotency and clear failure paths as much "
                           "as on the AI step itself.",
    "LLM engineering": "Measuring quality with evals and typed outputs makes prompt and model changes safer to ship.",
}


def score_relevance(ctx: dict[str, Any]) -> Relevance:
    item, topics = ctx["item"], ctx["topics"]
    text = f"{item['title']} {item['title']} {item['summary']}".lower()  # title counts double
    best_topic, best_hits, matched = None, 0, []
    for topic in topics:
        hits = [k for k in topic["keywords"] if re.search(rf"\b{re.escape(k.lower())}\b", text)]
        weighted = sum(text.count(k.lower()) for k in hits)
        if weighted > best_hits:
            best_topic, best_hits, matched = topic["name"], weighted, hits
    score = round(min(1.0, best_hits / 4), 2)
    if not best_topic:
        return Relevance(topic=None, score=0.0, rationale="No topic keywords found")
    return Relevance(topic=best_topic, score=score, rationale=f"Matches {', '.join(matched)}")


def summarise_item(ctx: dict[str, Any]) -> ItemSummary:
    item = ctx["item"]
    sentences = re.split(r"(?<=[.!?])\s+", item["summary"].strip())
    summary = " ".join(sentences[:2])
    return ItemSummary(headline=item["title"], summary=summary,
                       why_it_matters=IMPLICATIONS.get(ctx["topic"], "Worth tracking for upcoming client work."))


def write_brief(ctx: dict[str, Any]) -> Brief:
    sections: dict[str, list[dict]] = ctx["sections"]
    counts = ", ".join(f"{len(v)} on {k.lower()}" for k, v in sections.items() if v)
    total = sum(len(v) for v in sections.values())
    lead = max(sections, key=lambda k: len(sections[k])) if sections else "automation"
    intro = (f"This week's brief covers {total} items: {counts}. The strongest theme is {lead.lower()}, "
             "with a shared thread of moving AI features from prototypes to dependable production systems.")
    takeaways = [IMPLICATIONS[t] for t in sections if t in IMPLICATIONS][:3]
    return Brief(title=ctx["title"], intro=intro, takeaways=takeaways or ["No major updates this week."])


HANDLERS = {"score_relevance": score_relevance, "summarise_item": summarise_item, "write_brief": write_brief}
