"""Soft length limits for text the model writes.

Why: when a server enforces a JSON schema during generation (llama.cpp / Strata / LM Studio grammars), a hard
`maxLength` makes it end the string at exactly that many characters, even in the middle of a sentence. So we never SEND
maxLength; the model is told the limit in the description, and anything that still runs long is trimmed to the last
complete sentence here, on our side. Storage stays bounded and the prose stays whole.
"""
from __future__ import annotations

import re
from typing import Any

from annotated_types import MaxLen
from pydantic import BaseModel, model_validator

_SENTENCE_END = re.compile(r"[.!?][\"')\]]?(?=\s|$)")


def trim_text(text: Any, limit: int) -> str:
    """Cut to <= limit characters on a sentence boundary when possible, else a word boundary with an ellipsis."""
    text = str(text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = [m.end() for m in _SENTENCE_END.finditer(cut) if m.end() >= limit * 0.4]
    if ends:
        return cut[:max(ends)].strip()
    space = cut.rfind(" ")
    return (cut[:space] if space >= limit * 0.4 else cut[:limit - 1]).rstrip(" ,;:-") + "\u2026"


def soften(node: Any) -> Any:
    """Replace every string maxLength in a JSON schema with a plain-language hint."""
    if isinstance(node, dict):
        out = {k: soften(v) for k, v in node.items()}
        if out.get("type") == "string" and "maxLength" in out:
            n = out.pop("maxLength")
            hint = f"(at most {n} characters; end on a complete sentence)"
            out["description"] = f"{out['description']} {hint}" if out.get("description") else hint
        return out
    if isinstance(node, list):
        return [soften(v) for v in node]
    return node


class SoftModel(BaseModel):
    """BaseModel whose over-long strings are trimmed (not rejected) and whose schema carries no maxLength."""

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        return soften(super().model_json_schema(*args, **kwargs))

    @model_validator(mode="before")
    @classmethod
    def _trim_long_text(cls, data):
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for name, field in cls.model_fields.items():
            value = out.get(name)
            if isinstance(value, str):
                limit = next((m.max_length for m in field.metadata if isinstance(m, MaxLen)), None)
                if limit and len(value) > limit:
                    out[name] = trim_text(value, limit)
        return out
