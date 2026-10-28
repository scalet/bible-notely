from __future__ import annotations

import base64
import json
import mimetypes
import asyncio
import re
from pathlib import Path

import logging

import httpx

from backend.app.config import settings
from backend.app.runtime import get_ai_provider

log = logging.getLogger("bible_notely.google")


LLM_ATTEMPTS = 4
LLM_BACKOFF = 10
OPENAI_COMPAT_READ_TIMEOUT = 900
RETRY_STATUS = {408, 429, 500, 502, 503, 504, 529}


class LLMError(RuntimeError):
    pass


def _transient(message: str) -> LLMError:
    exc = LLMError(message)
    exc.transient = True
    return exc


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRY_STATUS
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, LLMError) and getattr(exc, "transient", False)


async def _with_retry(label: str, call, *, only_llm: bool = False):
    for attempt in range(1, LLM_ATTEMPTS + 1):
        try:
            return await call()
        except Exception as exc:
            if attempt == LLM_ATTEMPTS or not _retryable(exc) or (only_llm and not isinstance(exc, LLMError)):
                raise
            delay = LLM_BACKOFF * 2 ** (attempt - 1)
            log.warning(
                "%s falhou (%s: %s); tentativa %s/%s em %ss",
                label, type(exc).__name__, exc, attempt + 1, LLM_ATTEMPTS, delay,
            )
            await asyncio.sleep(delay)


def _model_for(provider: str) -> str:
    if settings.ai_model:
        return settings.ai_model
    return {
        "gemini": settings.gemini_model,
        "grok": settings.grok_model,
        "claude": settings.claude_model,
        "copilot": settings.copilot_model,
    }[provider]


def _b64(path: Path) -> tuple[str, str]:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return mime, base64.b64encode(path.read_bytes()).decode("ascii")


def _parse_json(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise _transient(f"Resposta da IA não é JSON: {text[:400]}")


async def generate(
    prompt: str,
    images: list[Path] | None = None,
    system: str | None = None,
) -> str:
    provider = get_ai_provider().lower()
    images = images or []
    if provider == "gemini":
        return await _with_retry("Gemini", lambda: _gemini(prompt, images, system))
    if provider == "claude":
        return await _with_retry("Claude", lambda: _claude(prompt, images, system))
    if provider in {"grok", "copilot"}:
        return await _with_retry(provider, lambda: _openai_compat(provider, prompt, images, system))
    raise LLMError(f"Provedor de IA desconhecido: {provider}")


async def generate_json(
    prompt: str,
    images: list[Path] | None = None,
    system: str | None = None,
) -> dict:
    async def attempt() -> dict:
        return _parse_json(await generate(prompt, images, system))

    return await _with_retry("resposta JSON da IA", attempt, only_llm=True)


async def _gemini(prompt: str, images: list[Path], system: str | None) -> str:
    model = _model_for("gemini")
    if not settings.gemini_api_key:
        log.error("chamada Gemini cancelada: GEMINI_API_KEY ausente")
        raise LLMError("GEMINI_API_KEY ausente no .env")
    log.info(
        "chamada Gemini modelo=%s imagens=%s via API key do AI Studio (não usa a conta OAuth do Drive)",
        model,
        len(images),
    )
    parts: list[dict] = []
    if system:
        parts.append({"text": system})
    for path in images:
        mime, data = _b64(path)
        parts.append({"inline_data": {"mime_type": mime, "data": data}})
    parts.append({"text": prompt})
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    timeout = httpx.Timeout(OPENAI_COMPAT_READ_TIMEOUT, connect=30)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            url,
            params={"key": settings.gemini_api_key},
            json={
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"temperature": 0.2},
            },
        )
    data = response.json()
    if response.status_code >= 400:
        message = ""
        if isinstance(data, dict):
            message = str((data.get("error") or {}).get("message") or "")
        log.error("Gemini recusou modelo=%s: HTTP %s %s", model, response.status_code, message)
        error = LLMError(message or f"Gemini HTTP {response.status_code}")
        error.transient = response.status_code in RETRY_STATUS
        raise error
    log.info("Gemini respondeu modelo=%s HTTP %s", model, response.status_code)
    try:
        chunks = [
            part.get("text", "")
            for part in data["candidates"][0]["content"]["parts"]
            if part.get("text")
        ]
        text = "".join(chunks).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Resposta Gemini inesperada: {data}") from exc
    if not text:
        raise _transient(f"Resposta Gemini sem texto: {data}")
    return text


async def _claude(prompt: str, images: list[Path], system: str | None) -> str:
    if not settings.anthropic_api_key:
        raise LLMError("ANTHROPIC_API_KEY ausente no .env")
    content: list[dict] = []
    for path in images:
        mime, data = _b64(path)
        content.append(
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
        )
    content.append({"type": "text", "text": prompt})
    timeout = httpx.Timeout(OPENAI_COMPAT_READ_TIMEOUT, connect=30)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _model_for("claude"),
                "max_tokens": 4000,
                "temperature": 0.2,
                "system": system or "Você é um assistente preciso.",
                "messages": [{"role": "user", "content": content}],
            },
        )
        response.raise_for_status()
        data = response.json()
    try:
        return next(b["text"] for b in data["content"] if b["type"] == "text")
    except StopIteration as exc:
        raise LLMError(f"Resposta Claude inesperada: {data}") from exc


async def _openai_compat(provider: str, prompt: str, images: list[Path], system: str | None) -> str:
    if provider == "grok":
        api_key, base, model = settings.grok_api_key, settings.grok_base_url, _model_for("grok")
        if not api_key:
            raise LLMError("GROK_API_KEY ausente no .env")
    else:
        api_key, base, model = (
            settings.copilot_api_key,
            settings.copilot_base_url,
            _model_for("copilot"),
        )
        if not api_key:
            raise LLMError("COPILOT_API_KEY ausente no .env")

    user_content: list[dict] | str
    if images:
        user_content = [{"type": "text", "text": prompt}]
        for path in images:
            mime, data = _b64(path)
            user_content.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
            )
    else:
        user_content = prompt

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_content})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if provider == "copilot":
        headers["Accept"] = "application/vnd.github+json"

    # Modelos de raciocínio (grok-4.x) podem levar vários minutos antes do 1º byte.
    timeout = httpx.Timeout(OPENAI_COMPAT_READ_TIMEOUT, connect=30)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{base.rstrip('/')}/chat/completions",
            headers=headers,
            json={"model": model, "messages": messages, "temperature": 0.2},
        )
    if response.status_code >= 400:
        log.error("%s recusou modelo=%s: HTTP %s %s", provider, model, response.status_code, response.text[:500])
        error = LLMError(f"{provider} HTTP {response.status_code}: {response.text[:300]}")
        error.transient = response.status_code in RETRY_STATUS
        raise error
    data = response.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise LLMError(f"Resposta {provider} inesperada: {data}") from exc


def provider_flags() -> dict[str, bool]:
    return {
        "gemini": bool(settings.gemini_api_key),
        "grok": bool(settings.grok_api_key),
        "claude": bool(settings.anthropic_api_key),
        "copilot": bool(settings.copilot_api_key),
    }
