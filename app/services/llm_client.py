"""LLM client with Anthropic primary and OpenAI fallback."""

from __future__ import annotations

import os
import requests


ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")


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


def generate_json_text(
    prompt: str,
    max_tokens: int = 2048,
    system_prompt: str | None = None,
) -> str:
    """Generate JSON text with Anthropic primary, OpenAI fallback."""
    errors = []

    try:
        return _call_anthropic_text(prompt, max_tokens=max_tokens, system_prompt=system_prompt)
    except Exception as e:
        errors.append(f"Anthropic failed: {e}")

    try:
        input_blocks = []
        if system_prompt:
            input_blocks.append({"role": "system", "content": [{"type": "input_text", "text": system_prompt}]})
        input_blocks.append({"role": "user", "content": [{"type": "input_text", "text": prompt}]})
        return _call_openai(input_blocks, max_tokens=max_tokens)
    except Exception as e:
        errors.append(f"OpenAI failed: {e}")

    raise RuntimeError("All LLM providers failed. " + " | ".join(errors))


def generate_json_with_image(
    system_prompt: str,
    user_text: str,
    image_data_b64: str,
    media_type: str,
    max_tokens: int = 16384,
) -> str:
    """Generate JSON with image input using Anthropic primary, OpenAI fallback."""
    errors = []

    try:
        return _call_anthropic_image(
            system_prompt=system_prompt,
            user_text=user_text,
            image_data_b64=image_data_b64,
            media_type=media_type,
            max_tokens=max_tokens,
        )
    except Exception as e:
        errors.append(f"Anthropic vision failed: {e}")

    try:
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
    except Exception as e:
        errors.append(f"OpenAI vision failed: {e}")

    raise RuntimeError("All LLM providers failed. " + " | ".join(errors))
