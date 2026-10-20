"""One place that talks to the model. Swap models by editing config.json, nothing else.

Works with any OpenAI-compatible endpoint (LM Studio, Ollama, llama.cpp server, Strata).
"""
from __future__ import annotations

import re
from typing import Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

from config import Config

T = TypeVar("T", bound=BaseModel)


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # tolerate chatter around the JSON object
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


class LLM:
    def __init__(self, cfg: Config):
        from openai import OpenAI  # imported here so tests can run without the SDK

        self.cfg = cfg
        self.client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key, timeout=cfg.timeout)

    def chat(self, messages: list[dict], tools: Optional[list[dict]] = None,
             effort: Optional[str] = None, schema: Optional[Type[BaseModel]] = None):
        """Return the assistant message object (has .content and .tool_calls)."""
        kwargs: dict = dict(model=self.cfg.model, messages=messages, temperature=self.cfg.temperature)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if schema is not None and self.cfg.use_json_schema_format:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
            }
        if effort and self.cfg.send_reasoning_effort:
            kwargs["extra_body"] = {"reasoning_effort": effort}
        return self.client.chat.completions.create(**kwargs).choices[0].message

    def structured(self, messages: list[dict], model_cls: Type[T],
                   effort: Optional[str] = None, retries: int = 2) -> Optional[T]:
        """Ask for JSON matching model_cls. Validate it. Return None if every attempt fails."""
        msgs = list(messages)
        for _ in range(retries + 1):
            try:
                msg = self.chat(msgs, effort=effort, schema=model_cls)
                raw = _strip_fences(msg.content or "")
                return model_cls.model_validate_json(raw)
            except (ValidationError, ValueError) as ex:
                msgs = msgs + [
                    {"role": "user",
                     "content": f"That was not valid. Reply with ONLY a JSON object matching the schema. Error: {str(ex)[:300]}"}
                ]
            except Exception:
                # network / server errors: let the caller's fail-soft path handle it
                return None
        return None
