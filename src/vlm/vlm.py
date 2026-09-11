"""Extract chart observations as raw text without a storage-schema constraint."""

from __future__ import annotations

import base64
import os

from openai import OpenAI
from parser.src.vlm.type_format import VlmContext

BASE_URL = os.getenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
API_KEY = os.getenv("OPENAI_API_KEY", "ollama")
DEFAULT_MODEL = os.getenv("VLM_MODEL", "qwen2.5vl")


def _client() -> OpenAI:
    """Build a client pointed at the configured endpoint."""
    return OpenAI(base_url=BASE_URL, api_key=API_KEY)


def _image_data_url(image: bytes) -> str:
    """Encode raw PNG bytes as a base64 data URL for the vision input."""
    return f"data:image/png;base64,{base64.b64encode(image).decode()}"


def extract_chart(
    image: bytes,
    context: VlmContext,
    *,
    model: str = DEFAULT_MODEL,
) -> str:
    """Return the model's original text for later preservation and formatting."""
    response = _client().chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": context["content"] or ""},
                    {
                        "type": "image_url",
                        "image_url": {"url": _image_data_url(image)},
                    },
                ],
            }
        ],
        temperature=0,
    )
    content = response.choices[0].message.content
    if content is None or not content.strip():
        raise ValueError("VLM returned no extraction text")
    return content
