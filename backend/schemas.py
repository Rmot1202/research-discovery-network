from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ReviewUpdate(BaseModel):
    approval_status: Literal["pending", "approved", "rejected"] | None = None
    summary: str | None = Field(default=None, max_length=1200)
    key_contribution: str | None = Field(default=None, max_length=1200)
    application: str | None = Field(default=None, max_length=1200)
    relevance: str | None = Field(default=None, max_length=1200)


class DocumentedAffiliation(BaseModel):
    institution: str = Field(min_length=1, max_length=300)
    country: str | None = Field(default=None, max_length=120)
    source_url: str = Field(min_length=8, max_length=1000)
    evidence: str = Field(min_length=10, max_length=2000)


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=6000)


class ChatRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=12)
    category: str | None = Field(default=None, max_length=12)
    limit: int = Field(default=8, ge=1, le=15)
