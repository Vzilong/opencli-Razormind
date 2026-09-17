"""Public request and result shapes for bounded project research."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator


class ResearchRunCreate(BaseModel):
    template_id: Literal["research-brief", "competitor-watch"]
    question: str = Field(default="", max_length=2_000)
    seed_urls: list[HttpUrl] = Field(default_factory=list, max_length=6)
    max_sources: int = Field(default=5, ge=1, le=6)
    request_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_template_inputs(self) -> ResearchRunCreate:
        self.question = self.question.strip()
        if self.template_id == "research-brief" and not self.question:
            raise ValueError("question is required for research-brief")
        if self.template_id == "competitor-watch" and not self.seed_urls:
            raise ValueError("seed_urls is required for competitor-watch")
        return self


class ResearchSource(BaseModel):
    id: str
    url: str
    title: str | None = None
    fetched_at: datetime
    content_hash: str | None = None
    excerpt: str = ""
    status: str
    watch_status: (
        Literal["baseline-established", "new-source", "unchanged", "changed", "fetch-failed"] | None
    ) = None
    baseline_run_id: str | None = None
    change_excerpt: dict[str, str] | None = None


class ResearchEvidenceQuote(BaseModel):
    source_id: str
    quote: str = Field(min_length=1, max_length=500)


class ResearchFinding(BaseModel):
    text: str
    source_ids: list[str]
    # Older persisted research rows predate quote-level evidence. New analysis
    # always emits at least one validated quote, while reads remain compatible.
    evidence: list[ResearchEvidenceQuote] = Field(default_factory=list, max_length=6)


class ResearchResult(BaseModel):
    summary: str
    findings: list[ResearchFinding] = Field(default_factory=list)
    sources: list[ResearchSource] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    changes: list[dict[str, Any]] = Field(default_factory=list)
    baseline_run_id: str | None = None


class ResearchRunRead(BaseModel):
    id: str
    workspace_id: str
    project_id: str
    template_id: str
    status: Literal["queued", "running", "completed", "partial", "failed"]
    created_at: datetime
    updated_at: datetime
    result: ResearchResult | None = None
    error: str | None = None
