from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class FeedItem(BaseModel):
    id: str
    feed: str
    title: str
    link: str
    summary: str
    published: datetime | None = None


class Relevance(BaseModel):
    """Analyst output for one item."""

    topic: str | None = Field(description="Best matching topic name, or null if none fit")
    score: float = Field(ge=0, le=1)
    rationale: str


class ItemSummary(BaseModel):
    """Analyst output: a short, neutral summary plus why it matters to the reader."""

    headline: str
    summary: str = Field(description="1-2 sentences, facts from the item only")
    why_it_matters: str = Field(description="One sentence on the practical implication for an automation team")


class BriefSection(BaseModel):
    topic: str
    items: list[tuple[FeedItem, ItemSummary]]


class Brief(BaseModel):
    title: str
    intro: str = Field(description="2-3 sentence overview of the week's themes")
    takeaways: list[str] = Field(description="3 short actionable takeaways", max_length=5)
