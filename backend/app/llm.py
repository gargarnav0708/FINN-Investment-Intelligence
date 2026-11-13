"""Two-tier model router with a persistent response cache.

FINN sends bulk, well-specified extraction work (reading a results table, pulling guidance
out of a transcript) to a cheap "fast" model and keeps the "strong" model for the one step
that needs judgement: comparing what management promised with what was delivered.

Every call is keyed by (tier model, prompt version, inputs) and stored in ``llm_calls``, so a
filing or transcript is only ever read by a model once; repeat requests are free.

Any OpenAI-compatible chat-completions endpoint works. Defaults target Google Gemini's
OpenAI-compatible endpoint (free tier: Flash-Lite for bulk work, Flash for reasoning).

Environment:
    FINN_LLM_API_KEY      (or GEMINI_API_KEY)   required to enable model features
    FINN_LLM_BASE_URL     default https://generativelanguage.googleapis.com/v1beta/openai
    FINN_LLM_FAST_MODEL   default gemini-3.5-flash-lite
    FINN_LLM_STRONG_MODEL default gemini-3.8-flash
    FINN_LLM_FAST_RPM / FINN_LLM_STRONG_RPM     client-side pacing (default 12 / 8)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import LLMCall

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
_pace_lock = threading.Lock()
_last_call: dict[str, float] = {}


class LLMUnavailable(RuntimeError):
    """Raised when no model is configured or the provider could not return usable JSON."""


@dataclass(frozen=True)
class TierConfig:
    tier: str
    model: str
    base_url: str
    api_key: str
    rpm: float
    reasoning_effort: str | None = None


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def tier_config(tier: str) -> TierConfig | None:
    key = _env("FINN_LLM_API_KEY") or _env("GEMINI_API_KEY")
    base = (_env("FINN_LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    if tier == "fast":
        model = _env("FINN_LLM_FAST_MODEL") or _env("FINN_LLM_MODEL") or "gemini-3.5-flash-lite"
        rpm = float(_env("FINN_LLM_FAST_RPM") or 12)
    elif tier == "strong":
        model = _env("FINN_LLM_STRONG_MODEL") or _env("FINN_LLM_MODEL") or "gemini-3.8-flash"
        rpm = float(_env("FINN_LLM_STRONG_RPM") or 8)
    else:
        raise ValueError(f"Unknown model tier: {tier}")
    if not key:
        return None
    # Gemini 2.5 "thinking" tokens count against max_tokens; keep it low on the reasoning tier and
    # off for bulk extraction. Only sent to Gemini unless set explicitly.
    effort = _env(f"FINN_LLM_{tier.upper()}_REASONING_EFFORT") or (
        ("low" if tier == "strong" else "none") if "googleapis.com" in base else "")
    return TierConfig(tier=tier, model=model, base_url=base, api_key=key, rpm=max(rpm, 0.1), reasoning_effort=effort or None)


def is_configured() -> bool:
    return tier_config("fast") is not None


def status() -> dict:
    fast, strong = tier_config("fast"), tier_config("strong")
    return {
        "configured": fast is not None,
        "fast_model": fast.model if fast else None,
        "strong_model": strong.model if strong else None,
        "base_url": fast.base_url if fast else None,
    }


def _cache_key(model: str, prompt_version: str, system: str, user: str, images: list[bytes] | None) -> str:
    digest = hashlib.sha256()
    for part in (model, prompt_version, system, user):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    for image in images or []:
        digest.update(hashlib.sha256(image).digest())
    return digest.hexdigest()


def _pace(config: TierConfig) -> None:
    interval = 60.0 / config.rpm
    with _pace_lock:
        wait = _last_call.get(config.model, 0.0) + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[config.model] = time.monotonic()


def parse_json_text(text: str) -> dict:
    """Parse a JSON object from a model reply, tolerating code fences and leading prose."""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("Model reply was JSON but not an object")
    return value


def _post(config: TierConfig, messages: list[dict], max_tokens: int, timeout: int) -> tuple[dict, dict]:
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    if config.reasoning_effort:
        payload["reasoning_effort"] = config.reasoning_effort
    headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
    last_error = "no attempt"
    for attempt in range(4):
        _pace(config)
        try:
            response = requests.post(f"{config.base_url}/chat/completions", json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(2 * (attempt + 1))
            continue
        if response.status_code == 429 or response.status_code >= 500:
            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            retry_after = response.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after and retry_after.replace('.', '', 1).isdigit() else 8 * (attempt + 1))
            continue
        if response.status_code == 400 and "reasoning_effort" in payload:
            payload.pop("reasoning_effort")  # model rejects this thinking setting; retry without it
            continue
        if response.status_code == 400 and "response_format" in payload:
            payload.pop("response_format")  # some models reject strict JSON mode; the prompt still asks for JSON
            continue
        if response.status_code >= 400:
            raise LLMUnavailable(f"HTTP {response.status_code}: {response.text[:500]}")
        body = response.json()
        content = body["choices"][0]["message"].get("content") or ""
        return parse_json_text(content), body.get("usage") or {}
    raise LLMUnavailable(f"Model provider unavailable after retries ({last_error})")


def complete_json(
    db: Session,
    *,
    tier: str,
    purpose: str,
    prompt_version: str,
    system: str,
    user: str,
    images: list[bytes] | None = None,
    max_tokens: int = 4096,
    timeout: int = 120,
    fallback_to_fast: bool = True,
) -> tuple[dict, dict]:
    """Return (parsed JSON, call metadata). Cached by inputs; raises LLMUnavailable."""
    config = tier_config(tier)
    if config is None:
        raise LLMUnavailable("No model provider is configured (set FINN_LLM_API_KEY).")
    key = _cache_key(config.model, prompt_version, system, user, images)
    cached = db.scalar(select(LLMCall).where(LLMCall.cache_key == key))
    if cached is not None and cached.status == "success" and cached.response_json is not None:
        cached.cache_hits += 1
        db.commit()
        return cached.response_json, {"model": cached.model, "tier": cached.tier, "cached": True, "call_id": cached.id}

    content: list[dict] | str
    if images:
        content = [{"type": "text", "text": user}] + [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image).decode()}}
            for image in images
        ]
    else:
        content = user
    messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    started = time.monotonic()
    try:
        parsed, usage = _post(config, messages, max_tokens, timeout)
    except LLMUnavailable as exc:
        if tier == "strong" and fallback_to_fast and tier_config("fast") and tier_config("fast").model != config.model:
            # Free-tier quotas on the reasoning model are small; degrade rather than fail the workflow.
            result, meta = complete_json(db, tier="fast", purpose=purpose, prompt_version=prompt_version, system=system,
                                         user=user, images=images, max_tokens=max_tokens, timeout=timeout, fallback_to_fast=False)
            return result, {**meta, "degraded_from": config.model}
        _record(db, key, purpose, config, prompt_version, "error", None, str(exc), {}, started)
        raise
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        _record(db, key, purpose, config, prompt_version, "error", None, f"Unparseable reply: {exc}", {}, started)
        raise LLMUnavailable(f"Model reply could not be parsed as JSON: {exc}") from exc
    row = _record(db, key, purpose, config, prompt_version, "success", parsed, None, usage, started)
    return parsed, {"model": config.model, "tier": tier, "cached": False, "call_id": row.id}


def _record(db: Session, key: str, purpose: str, config: TierConfig, prompt_version: str, status: str,
            parsed: dict | None, error: str | None, usage: dict, started: float) -> LLMCall:
    row = db.scalar(select(LLMCall).where(LLMCall.cache_key == key))
    if row is None:
        row = LLMCall(cache_key=key, purpose=purpose, tier=config.tier, model=config.model, prompt_version=prompt_version, status=status)
        db.add(row)
    row.status, row.response_json, row.error = status, parsed, error
    row.input_tokens = int(usage.get("prompt_tokens") or 0)
    row.output_tokens = int(usage.get("completion_tokens") or 0)
    row.latency_ms = int((time.monotonic() - started) * 1000)
    db.commit()
    return row


def usage_summary(db: Session) -> dict:
    rows = db.scalars(select(LLMCall)).all()
    by_purpose: dict[str, dict] = {}
    for row in rows:
        bucket = by_purpose.setdefault(row.purpose, {"calls": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0, "errors": 0, "models": set()})
        bucket["calls"] += 1
        bucket["cache_hits"] += row.cache_hits
        bucket["input_tokens"] += row.input_tokens
        bucket["output_tokens"] += row.output_tokens
        bucket["errors"] += row.status != "success"
        bucket["models"].add(row.model)
    for bucket in by_purpose.values():
        bucket["models"] = sorted(bucket["models"])
    return {
        **status(),
        "total_calls": len(rows),
        "total_cache_hits": sum(r.cache_hits for r in rows),
        "input_tokens": sum(r.input_tokens for r in rows),
        "output_tokens": sum(r.output_tokens for r in rows),
        "by_purpose": by_purpose,
    }
