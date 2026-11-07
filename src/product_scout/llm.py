"""LLM clients: any OpenAI-compatible endpoint (stdlib only), or Claude via the official Anthropic SDK."""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import urlparse

from .net import Http


class LLMError(Exception):
    pass


class RefusalError(LLMError):
    """The model declined to evaluate this product (e.g. exploit code)."""


def provider_for(cfg: dict[str, Any]) -> str:
    provider = str(cfg.get("provider", "auto")).lower()
    if provider != "auto":
        return provider
    host = (urlparse(str(cfg.get("base_url", ""))).hostname or "").lower()
    return "anthropic" if host == "api.anthropic.com" else "openai"


def make_llm(http: Http, cfg: dict[str, Any], proxy: str | None = None) -> "OpenAICompatible | AnthropicLLM":
    if provider_for(cfg) == "anthropic":
        return AnthropicLLM(cfg, proxy)
    return OpenAICompatible(http, cfg)


def _api_key(cfg: dict[str, Any]) -> str:
    return os.environ.get(cfg.get("api_key_env") or "SCOUT_LLM_API_KEY", "")


class OpenAICompatible:
    def __init__(self, http: Http, cfg: dict[str, Any]):
        self.http = http
        self.base_url = str(cfg.get("base_url", "")).rstrip("/")
        self.model = cfg.get("model", "")
        self.temperature = float(cfg.get("temperature", 0.5))
        self.api_key = _api_key(cfg)

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    def chat(self, system: str, user: str, *, json_mode: bool = True) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self.http.post_json(
            f"{self.base_url}/chat/completions",
            payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=120,
        )
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected response: {str(data)[:300]}") from e

    def chat_json(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        return extract_json(self.chat(system, user))


# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLM:
    """Claude through the official SDK (`pip install anthropic`).

    Uses structured outputs so replies are always valid JSON, and sends no sampling
    parameters (current Claude models reject non-default temperature).
    """

    def __init__(self, cfg: dict[str, Any], proxy: str | None = None):
        self.model = cfg.get("model", "")
        self.effort = cfg.get("effort", "low")
        self.fallback = bool(cfg.get("refusal_fallback", True)) and self.model in _FALLBACK_MODELS
        self.api_key = _api_key(cfg)
        self.client = None
        self.import_error = ""
        try:
            import anthropic
        except ImportError:
            self.import_error = "需要先安装 Anthropic SDK：pip install anthropic"
            return
        base_url = str(cfg.get("base_url", "")).rstrip("/")
        base_url = re.sub(r"/v1$", "", base_url) or None  # the SDK adds /v1 itself
        kwargs: dict[str, Any] = {"api_key": self.api_key or None, "base_url": base_url, "max_retries": 3}
        if proxy:
            kwargs["http_client"] = anthropic.DefaultHttpxClient(proxy=proxy)
        self.client = anthropic.Anthropic(**kwargs)

    @property
    def available(self) -> bool:
        return bool(self.client and self.model and self.api_key)

    def chat_json(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        import anthropic

        if self.client is None:
            raise LLMError(self.import_error)
        output_config: dict[str, Any] = {"effort": self.effort}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        request = dict(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
        )
        try:
            if self.fallback:
                response = self.client.beta.messages.create(
                    **request, betas=[_FALLBACK_BETA], fallbacks="default"
                )
            else:
                response = self.client.messages.create(**request)
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic API Key 无效") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"找不到模型 {self.model}") from e
        except anthropic.RateLimitError as e:
            raise LLMError("触发限流，稍后再试") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"API 错误 {e.status_code}：{e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"连不上 Anthropic API：{e}") from e
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) if response.stop_details else None
            raise RefusalError(f"模型拒绝回答（{category}）")
        if response.stop_reason == "max_tokens":
            raise LLMError("输出被截断（max_tokens）")
        text = next((b.text for b in response.content if b.type == "text"), "")
        return extract_json(text)


def extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object out of a model reply, tolerating code fences and surrounding prose."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        obj = json.loads(text)
    except ValueError as e:
        raise LLMError(f"model did not return JSON: {text[:200]}") from e
    if not isinstance(obj, dict):
        raise LLMError("model returned JSON that is not an object")
    return obj
