"""Versioned prompt loading with deterministic content hashes."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from opspilot.llm.models import PromptTemplate


def load_prompt(
    path: Path,
    *,
    component: str,
    version: str,
    model_family: str | None = None,
) -> PromptTemplate:
    content = path.read_text(encoding="utf-8")
    return PromptTemplate(
        component=component,
        version=version,
        content=content,
        content_hash=sha256(content.encode("utf-8")).hexdigest(),
        model_family=model_family,
    )
