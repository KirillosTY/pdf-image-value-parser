"""Invoke configured vision and formatting endpoints with bounded requests."""

import base64
import json

from jsonschema import Draft202012Validator
from openai import AsyncOpenAI
from parser.src.formatter.model import (
    FORMAT_INSTRUCTIONS,
    FormattingError,
    NuExtractFormatter,
    normalize_fields,
    parse_formatter_json,
)
from parser.src.vlm.type_format import DEFAULT_PROMT

from agent.resources import endpoint_options


async def extract(model: dict, png: bytes, context: dict) -> str:
    """Return complete free-form observations and preserve their original text."""
    async with AsyncOpenAI(**endpoint_options(model)) as client:
        response = await client.chat.completions.create(
            model=model["model_id"],
            temperature=0,
            max_tokens=model.get("max_output_tokens") or 16384,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": DEFAULT_PROMT
                            + "\nSource context:\n"
                            + json.dumps(context),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(png).decode()
                            },
                        },
                    ],
                }
            ],
        )
    if not response.choices:
        raise ValueError("VLM returned no response")
    choice = response.choices[0]
    raw = choice.message.content
    if choice.finish_reason != "stop" or not raw or not raw.strip():
        raise ValueError("VLM did not return complete observations")
    return raw


async def format_result(
    model: dict, raw: str, schema: dict, context: dict, previous_error=None
) -> dict:
    """Validate generic chat output or use the explicit NuExtract adapter."""
    if model.get("formatter_adapter") == "nuextract":
        import asyncio

        from openai import OpenAI

        def invoke():
            with OpenAI(**endpoint_options(model)) as client:
                return NuExtractFormatter(
                    client, model["model_id"], model.get("max_output_tokens") or 16384
                ).format(
                    raw, schema, validation_error=previous_error, source_context=context
                )

        return await asyncio.to_thread(invoke)
    response_mode = model.get("formatter_response_format", "text")
    envelope = {
        "type": "object",
        "properties": {
            "data": schema,
            "unmapped_observations": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["data", "unmapped_observations"],
        "additionalProperties": False,
    }
    response_options = {}
    if response_mode == "json_schema":
        response_options["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "formatted_observations", "schema": envelope},
        }
    elif response_mode == "json_object":
        response_options["response_format"] = {"type": "json_object"}
    async with AsyncOpenAI(**endpoint_options(model)) as client:
        response = await client.chat.completions.create(
            model=model["model_id"],
            temperature=0,
            max_tokens=model.get("max_output_tokens") or 16384,
            **response_options,
            messages=[
                {
                    "role": "system",
                    "content": FORMAT_INSTRUCTIONS
                    + '\nReturn {"data": <value matching the schema>, "unmapped_observations": [<strings>]}. ',
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "observations": raw,
                            "schema": schema,
                            "source_context": context,
                            "previous_validation_error": previous_error,
                        }
                    ),
                },
            ],
        )
    if not response.choices:
        raise FormattingError("Formatter returned no response", "")
    choice = response.choices[0]
    text = choice.message.content or ""
    try:
        if choice.finish_reason != "stop":
            raise ValueError("Formatter response was truncated")
        result = parse_formatter_json(text)
        if not isinstance(result, dict) or set(result) != {
            "data",
            "unmapped_observations",
        }:
            raise ValueError("Unexpected formatter response structure")
        if not isinstance(result["unmapped_observations"], list) or not all(
            isinstance(value, str) for value in result["unmapped_observations"]
        ):
            raise ValueError("unmapped_observations must contain strings")
        result["data"] = normalize_fields(result["data"], schema)
        Draft202012Validator(schema).validate(result["data"])
    except Exception as exc:
        raise FormattingError(str(exc), text) from exc
    return {
        **result,
        "model": model["model_id"],
        "schema": schema,
        "model_response": text,
        "error": None,
    }
