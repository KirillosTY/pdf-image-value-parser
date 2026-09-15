"""Call the configured MainAgent endpoint."""

import os
import json
from dataclasses import replace

from openai import AsyncOpenAI

from agent.state import State


async def ask_main_agent(state: State, instruction: str) -> str:
    """Return one complete response from the configured MainAgent."""
    registry = state.config["models"]
    agent_key = state.config.get("main_agent")
    model = registry.get(agent_key)

    if model is None or "main_agent" not in model["roles"]:
        raise ValueError("Configure a model with the main_agent role")

    if not state.system_prompt:
        raise ValueError("Prepare the MainAgent system prompt first")

    endpoint_env = model.get("endpoint_env")
    if not endpoint_env:
        raise ValueError("Configure the MainAgent's endpoint_env")

    key_env = model.get("api_key_env")
    api_key = os.environ[key_env] if key_env else "EMPTY"

    request = {
        "model": model["model_id"],
        "messages": [
            {"role": "system", "content": state.system_prompt},
            {"role": "user", "content": instruction},
        ],
    }

    if model.get("max_output_tokens") is not None:
        request["max_tokens"] = model["max_output_tokens"]

    async with AsyncOpenAI(
        base_url=os.environ[endpoint_env],
        api_key=api_key,
        timeout=120,
        max_retries=0,
    ) as client:
        response = await client.chat.completions.create(**request)

    if not response.choices:
        raise ValueError("MainAgent returned no response")

    choice = response.choices[0]
    text = choice.message.content

    if choice.finish_reason != "stop" or not text or not text.strip():
        raise ValueError("MainAgent did not return a complete response")

    return text


async def ask_main_agent_tool(state: State, tools: list[dict], context: str) -> dict:
    """Request exactly one native function call from the configured MainAgent."""
    from agent.resources import endpoint_options

    model = state.config["models"].get(state.config.get("main_agent"))
    if model is None or "main_agent" not in model["roles"]:
        raise ValueError("Configure a model with the main_agent role")
    if not state.system_prompt:
        raise ValueError("Prepare the MainAgent system prompt first")
    request = {
        "model": model["model_id"],
        "messages": [
            {"role": "system", "content": state.system_prompt},
            {"role": "user", "content": "Execute this approved run through the pipeline tools."},
            *state.agent_messages,
            {"role": "user", "content": context},
        ],
        "tools": tools,
        "tool_choice": "required",
        "parallel_tool_calls": False,
        "max_tokens": model.get("max_output_tokens") or 4096,
    }
    async with AsyncOpenAI(**endpoint_options(model)) as client:
        response = await client.chat.completions.create(**request)
    if not response.choices:
        raise ValueError("MainAgent returned no response")
    choice = response.choices[0]
    calls = choice.message.tool_calls
    # Some compatible servers return 'stop' alongside native tool_calls.
    if choice.finish_reason not in {"tool_calls", "stop"} or not calls or len(calls) != 1:
        raise ValueError("MainAgent must return exactly one complete tool call")
    call = calls[0]
    if call.type != "function" or not call.id:
        raise ValueError("MainAgent must return an identified function call")
    return {
        "role": "assistant", "content": choice.message.content,
        "tool_calls": [call.model_dump()],
    }


async def choose_model_with_tool(state: State, name: str, allowed: set[str], context: str) -> str:
    """Select one queue model through a validated native MainAgent tool call."""
    spec = {"type": "function", "function": {
        "name": name,
        "description": "Select the best allowed model for the supplied source data and approved run.",
        "parameters": {
            "type": "object", "properties": {"model": {"type": "string", "enum": sorted(allowed)}},
            "required": ["model"], "additionalProperties": False,
        },
    }}
    request_state = replace(state, agent_messages=[])
    for _ in range(3):
        message = None
        try:
            message = await ask_main_agent_tool(request_state, [spec], context)
            call = message["tool_calls"][0]
            if call["function"]["name"] != name:
                raise ValueError("Use the offered routing tool")

            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError(f"Duplicate routing argument: {key}")
                    result[key] = value
                return result

            arguments = json.loads(call["function"]["arguments"], object_pairs_hook=unique_object)
            if (not isinstance(arguments, dict) or set(arguments) != {"model"}
                    or not isinstance(arguments["model"], str) or arguments["model"] not in allowed):
                raise ValueError("Select exactly one allowed model registry key")
            return arguments["model"]
        except (ValueError, TypeError) as exc:
            if message:
                request_state.agent_messages.extend([message, {
                    "role": "tool", "tool_call_id": message["tool_calls"][0]["id"],
                    "content": json.dumps({"ok": False, "error": str(exc)}),
                }])
            else:
                request_state.agent_messages.append({"role": "user", "content": str(exc)})
    raise ValueError("MainAgent could not choose a valid queue model after 3 attempts")
