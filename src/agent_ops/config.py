from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class Topic(BaseModel):
    name: str
    keywords: list[str]


class Feed(BaseModel):
    name: str
    url: str


class Output(BaseModel):
    markdown_dir: str = "runs"
    slack_webhook_env: str = "SLACK_WEBHOOK_URL"


class PipelineConfig(BaseModel):
    name: str
    schedule: str = "0 8 * * MON"
    timezone: str = "UTC"
    max_items_per_topic: int = Field(3, ge=1, le=20)
    min_relevance: float = Field(0.5, ge=0, le=1)
    lookback_days: int = Field(0, ge=0)
    topics: list[Topic]
    feeds: list[Feed]
    output: Output = Output()
    base_dir: Path = Path(".")

    def resolve(self, value: str) -> str:
        """Feed URLs and output dirs may be relative to the config file."""
        if value.startswith(("http://", "https://")):
            return value
        return str((self.base_dir / value).resolve())

    @property
    def runs_dir(self) -> Path:
        return Path(self.resolve(self.output.markdown_dir))


def load_config(path: Path) -> PipelineConfig:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return PipelineConfig(**data, base_dir=path.resolve().parent)
