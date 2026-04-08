"""LLM client with Anthropic primary and OpenAI fallback."""

from __future__ import annotations

import os
import requests
from functools import wraps


ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")


def _log_provider(message: str) -> None:
    """Emit a consistent provider log line."""
    print(f"  [LLM] {message}")


def _with_provider_fallback(fn):
    """Decorator: run Anthropic first, then OpenAI fallback with consistent logs."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        task_label = kwargs.get("task_label", "llm-task")
        plan = fn(*args, **kwargs)
        errors = []

        try:
            text = plan["anthropic"]()
            _log_provider(f"{task_label}: using Anthropic ({os.getenv('ANTHROPIC_MODEL', ANTHROPIC_MODEL)})")
            return text
        except Exception as e:
            _log_provider(f"{task_label}: Anthropic failed ({e}) -> trying OpenAI fallback")
            errors.append(f"{plan['anthropic_error']}: {e}")

        try:
            text = plan["openai"]()
            _log_provider(f"{task_label}: using OpenAI ({os.getenv('OPENAI_MODEL', OPENAI_MODEL)})")
            return text
        except Exception as e:
            _log_provider(f"{task_label}: OpenAI fallback failed ({e})")
            errors.append(f"{plan['openai_error']}: {e}")

        raise RuntimeError("All LLM providers failed. " + " | ".join(errors))

    return wrapper


def _extract_openai_text(data: dict) -> str:
    """Extract plain text from OpenAI Responses or Chat Completions payloads."""
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    parts = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in ("output_text", "text"):
                txt = content.get("text")
                if isinstance(txt, str):
                    parts.append(txt)

    if parts:
        return "\n".join(parts).strip()

    choices = data.get("choices", [])
    if choices:
        content = choices[0].get("message", {}).get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            chunks = []
            for c in content:
                txt = c.get("text")
                if isinstance(txt, str):
                    chunks.append(txt)
            if chunks:
                return "\n".join(chunks).strip()

    raise RuntimeError("No text found in OpenAI response payload.")


def _call_openai(input_blocks: list[dict], max_tokens: int) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set.")

    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": os.getenv("OPENAI_MODEL", OPENAI_MODEL),
            "input": input_blocks,
            "temperature": 0,
            "max_output_tokens": max_tokens,
        },
        timeout=120,
    )
    response.raise_for_status()
    return _extract_openai_text(response.json())


def _call_anthropic_text(prompt: str, max_tokens: int, system_prompt: str | None = None) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    kwargs = {
        "model": os.getenv("ANTHROPIC_MODEL", ANTHROPIC_MODEL),
        "max_tokens": max_tokens,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system_prompt:
        kwargs["system"] = system_prompt

    resp = client.messages.create(**kwargs)
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Anthropic response truncated at max_tokens.")
    return resp.content[0].text


def _call_anthropic_image(
    system_prompt: str,
    user_text: str,
    image_data_b64: str,
    media_type: str,
    max_tokens: int,
) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=os.getenv("ANTHROPIC_MODEL", ANTHROPIC_MODEL),
        max_tokens=max_tokens,
        temperature=0,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data_b64}},
                {"type": "text", "text": user_text},
            ],
        }],
    )
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Anthropic response truncated at max_tokens.")
    return resp.content[0].text


@_with_provider_fallback
def generate_json_text(
    prompt: str,
    max_tokens: int = 2048,
    system_prompt: str | None = None,
    task_label: str = "text-json",
) -> dict:
    """Generate JSON text with Anthropic primary, OpenAI fallback."""
    def _openai_call() -> str:
        input_blocks = []
        if system_prompt:
            input_blocks.append({"role": "system", "content": [{"type": "input_text", "text": system_prompt}]})
        input_blocks.append({"role": "user", "content": [{"type": "input_text", "text": prompt}]})
        return _call_openai(input_blocks, max_tokens=max_tokens)

    return {
        "anthropic": lambda: _call_anthropic_text(prompt, max_tokens=max_tokens, system_prompt=system_prompt),
        "openai": _openai_call,
        "anthropic_error": "Anthropic failed",
        "openai_error": "OpenAI failed",
    }


@_with_provider_fallback
def generate_json_with_image(
    system_prompt: str,
    user_text: str,
    image_data_b64: str,
    media_type: str,
    max_tokens: int = 16384,
    task_label: str = "vision-json",
) -> dict:
    """Generate JSON with image input using Anthropic primary, OpenAI fallback."""
    def _anthropic_call() -> str:
        return _call_anthropic_image(
            system_prompt=system_prompt,
            user_text=user_text,
            image_data_b64=image_data_b64,
            media_type=media_type,
            max_tokens=max_tokens,
        )

    def _openai_call() -> str:
        input_blocks = [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user_text},
                    {"type": "input_image", "image_url": f"data:{media_type};base64,{image_data_b64}"},
                ],
            },
        ]
        return _call_openai(input_blocks, max_tokens=max_tokens)

    return {
        "anthropic": _anthropic_call,
        "openai": _openai_call,
        "anthropic_error": "Anthropic vision failed",
        "openai_error": "OpenAI vision failed",
    }
