"""
LLM provider dispatch.

Follows the house pattern from ``astro_eval/analysis.py``: a single
``"provider/model-id"`` string, SDKs imported lazily inside the call so the
package installs without any of them, and API keys read from the environment.

Four of the five providers are OpenAI-compatible and differ only in base URL,
so OpenRouter and Ollama Cloud slot in beside local Ollama with no new client
code:

============  =========================================  ====================
provider      base_url                                   key
============  =========================================  ====================
ollama        {ollama_url}/v1                             (none needed)
ollama_cloud  https://ollama.com/v1                       OLLAMA_API_KEY
openrouter    https://openrouter.ai/api/v1                OPENROUTER_API_KEY
openai        (default)                                   OPENAI_API_KEY
anthropic     native SDK                                  ANTHROPIC_API_KEY
============  =========================================  ====================

Note that an OpenRouter model id itself contains a slash
(``anthropic/claude-sonnet-5``), so the provider is split off with
``partition`` and the remainder is passed through untouched.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OLLAMA_CLOUD_BASE = "https://ollama.com/v1"

PROVIDERS = ("ollama", "ollama_cloud", "openrouter", "openai", "anthropic")


class LlmError(RuntimeError):
    """The model could not be reached or refused the request."""


@dataclass
class LlmReply:
    text: str
    latency_ms: int
    model: str
    prompt_chars: int


def split_model(model_str: str) -> tuple[str, str]:
    """
    Split ``provider/model-id``.

    Only the first slash is consumed, so OpenRouter ids that contain their own
    slash survive intact.
    """
    provider, sep, model = model_str.partition("/")
    provider = provider.strip().lower()
    if not sep or not model.strip():
        raise LlmError(
            f"invalid model string {model_str!r}; expected 'provider/model-id', e.g.\n"
            "  ollama/llama3.1:8b\n"
            "  ollama_cloud/gpt-oss:120b\n"
            "  openrouter/anthropic/claude-sonnet-5\n"
            "  anthropic/claude-opus-5"
        )
    if provider not in PROVIDERS:
        raise LlmError(
            f"unknown provider {provider!r}; expected one of {', '.join(PROVIDERS)}"
        )
    return provider, model.strip()


def call_llm(
    model_str: str,
    system: str,
    prompt: str,
    *,
    max_tokens: int = 1500,
    timeout_s: float = 120.0,
    ollama_url: str = "http://localhost:11434",
    force_json: bool = True,
) -> LlmReply:
    """
    Send one request and return the raw text.

    Blocking on purpose -- the caller runs it in a worker thread so the event
    loop keeps serving telemetry while a local model thinks.
    """
    provider, model = split_model(model_str)
    started = time.monotonic()

    if provider == "anthropic":
        text = _call_anthropic(model, system, prompt, max_tokens, timeout_s)
    else:
        base_url, api_key = _openai_target(provider, ollama_url)
        text = _call_openai_compat(
            model, system, prompt, api_key, max_tokens, timeout_s,
            base_url=base_url, force_json=force_json,
        )

    return LlmReply(
        text=text,
        latency_ms=int((time.monotonic() - started) * 1000),
        model=model_str,
        prompt_chars=len(prompt) + len(system),
    )


def _openai_target(provider: str, ollama_url: str) -> tuple[Optional[str], str]:
    if provider == "ollama":
        return ollama_url.rstrip("/") + "/v1", "ollama"
    if provider == "ollama_cloud":
        key = os.environ.get("OLLAMA_API_KEY", "").strip()
        if not key:
            raise LlmError(
                "OLLAMA_API_KEY is not set.\n"
                "Add it to .env (get one at https://ollama.com/settings/keys), "
                "or switch [llm] model to an ollama/ model."
            )
        return OLLAMA_CLOUD_BASE, key
    if provider == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise LlmError(
                "OPENROUTER_API_KEY is not set.\n"
                "Add it to .env, or switch [llm] model to an ollama/ model."
            )
        return OPENROUTER_BASE, key
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise LlmError("OPENAI_API_KEY is not set.")
    return None, key


def _call_openai_compat(
    model: str,
    system: str,
    prompt: str,
    api_key: str,
    max_tokens: int,
    timeout_s: float,
    *,
    base_url: Optional[str] = None,
    force_json: bool = True,
) -> str:
    try:
        import openai  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise LlmError(
            "the 'openai' package is not installed.\n"
            "Install it with:  uv sync --extra llm"
        ) from exc

    kwargs: dict = {"api_key": api_key, "timeout": timeout_s}
    if base_url:
        kwargs["base_url"] = base_url
    client = openai.OpenAI(**kwargs)

    request: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    if force_json:
        # An 8B model is unreliable at free-form JSON; asking the server to
        # constrain the output is far more effective than asking politely in
        # the prompt. Not every backend supports it, so a rejection falls back
        # to an unconstrained call rather than failing the tick.
        request["response_format"] = {"type": "json_object"}

    try:
        resp = client.chat.completions.create(**request)
    except Exception as exc:  # noqa: BLE001 - SDK raises a wide variety
        if force_json and _is_unsupported_response_format(exc):
            log.debug("response_format unsupported by %s; retrying without it", model)
            request.pop("response_format", None)
            try:
                resp = client.chat.completions.create(**request)
            except Exception as retry_exc:  # noqa: BLE001
                raise LlmError(f"{model}: {retry_exc}") from retry_exc
        else:
            raise LlmError(f"{model}: {exc}") from exc

    try:
        return resp.choices[0].message.content or ""
    except (AttributeError, IndexError) as exc:
        raise LlmError(f"{model}: unexpected response shape") from exc


def _is_unsupported_response_format(exc: Exception) -> bool:
    text = str(exc).lower()
    return "response_format" in text or "json_object" in text


def _call_anthropic(
    model: str,
    system: str,
    prompt: str,
    max_tokens: int,
    timeout_s: float,
) -> str:
    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise LlmError(
            "the 'anthropic' package is not installed.\n"
            "Install it with:  uv sync --extra anthropic"
        ) from exc

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise LlmError("ANTHROPIC_API_KEY is not set.")

    client = anthropic.Anthropic(api_key=key, timeout=timeout_s)
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001
        raise LlmError(f"{model}: {exc}") from exc

    return "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    )


# ── response parsing ───────────────────────────────────────────────────

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_THINK = re.compile(r"<(think|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Optional[dict]:
    """
    Pull one JSON object out of a model reply.

    Small models wrap JSON in prose or code fences and sometimes emit a
    trailing explanation. This tries the whole string, then any fenced block,
    then the first balanced ``{...}`` span. Anything still unparseable returns
    None -- the caller treats that as "do nothing", never as a retry loop.

    Reasoning models (gpt-oss and similar) can precede the answer with a
    complete ``<think>``/``<reasoning>`` block; that is stripped first so the
    balanced-brace scan finds the real answer rather than an example
    structure mentioned while thinking out loud. A response that runs out of
    tokens mid-thought -- no closing tag, no answer at all -- still correctly
    yields None here; that is a `max_tokens` problem, not a parsing one.
    """
    if not text:
        return None
    text = _THINK.sub("", text)
    if not text.strip():
        return None
    candidates = [text.strip()]
    candidates.extend(m.group(1).strip() for m in _FENCE.finditer(text))
    span = _balanced_span(text)
    if span:
        candidates.append(span)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _balanced_span(text: str) -> Optional[str]:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None
