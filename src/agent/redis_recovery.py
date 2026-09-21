"""Diagnose Redis and start only its configured, existing local Compose service."""

import asyncio
import json
import os
import signal
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from parser.src.redis.connection import async_redis_client, redis_url
from redis.exceptions import AuthenticationError, ConnectionError, TimeoutError

from agent.diagnostic_redaction import redact_diagnostic

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def matches_local_service(policy):
    """Require the Redis connection to target the explicitly configured local port."""
    target = urlsplit(redis_url())
    return (
        policy.get("enabled", False)
        and target.scheme == "redis"
        and target.hostname in {"localhost", "127.0.0.1", "::1"}
        and (target.port or 6379) == policy["port"]
    )


async def probe_connection():
    """Report connection, authentication and timeout failures without using queues."""
    try:
        async with async_redis_client() as client:
            await client.ping()
        return {"reachable": True, "kind": "ready"}
    except Exception as exc:
        kind = ("authentication" if isinstance(exc, AuthenticationError)
                else "timeout" if isinstance(exc, TimeoutError)
                else "connection" if isinstance(exc, (ConnectionError, OSError))
                else "configuration_or_protocol")
        return {"reachable": False, "kind": kind,
                "error": redact_diagnostic(f"{type(exc).__name__}: {exc}")}


async def run_compose(policy, arguments):
    """Execute a fixed service action without model-supplied commands or shell parsing."""
    command = [
        "docker", "compose", "--env-file", policy["env_file"],
        "-p", policy["project"], "-f", policy["compose_file"],
        *arguments, policy["service"],
    ]
    with tempfile.TemporaryFile() as output:
        process = await asyncio.create_subprocess_exec(
            *command, cwd=PROJECT_ROOT, start_new_session=True,
            stdout=output, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=45)
            output.seek(0, os.SEEK_END)
            output.seek(max(0, output.tell() - 16000))
            result = output.read().decode(errors="replace")
            if process.returncode:
                raise RuntimeError(redact_diagnostic(result))
            return result
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()


async def service_status(policy):
    """Read only the configured service, accepting Compose JSON arrays or JSON Lines."""
    raw = await run_compose(policy, ["ps", "--all", "--format", "json"])
    try:
        rows = json.loads(raw) if raw.strip() else []
        if isinstance(rows, dict):
            rows = [rows]
    except json.JSONDecodeError:
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    return [{key: row.get(key) for key in ("Name", "State", "Health", "ExitCode")}
            for row in rows if row.get("Service") == policy["service"]
            and row.get("Project") == policy["project"]]


async def diagnose_redis(policy):
    """Return ping evidence, service state and a bounded log tail to the MainAgent."""
    result = await probe_connection()
    result["repair_available"] = False
    if not matches_local_service(policy):
        result["service_note"] = "No recovery service configured for this Redis endpoint."
        return result
    try:
        containers = await service_status(policy)
        result["containers"] = containers
        result["repair_available"] = (
            result["kind"] == "connection" and bool(containers)
            and all(item["State"] in {"exited", "created"} for item in containers)
        )
        result["service_logs"] = redact_diagnostic(
            await run_compose(policy, ["logs", "--no-color", "--tail", "30"])
        )
        if not containers:
            result["service_note"] = "The configured container does not exist; provision it before retrying."
    except Exception as exc:
        result["service_error"] = redact_diagnostic(f"{type(exc).__name__}: {exc}")
    return result


async def start_redis_service(policy):
    """Start an existing stopped Redis container, then verify the actual connection."""
    if not matches_local_service(policy):
        raise ValueError("Redis endpoint does not match the configured local recovery service")
    connection = await probe_connection()
    if connection["reachable"]:
        return {**connection, "action": "already_ready"}
    containers = await service_status(policy)
    if (connection["kind"] != "connection" or not containers
            or any(item["State"] not in {"exited", "created"} for item in containers)):
        raise RuntimeError("Redis is not an existing stopped service; inspect diagnostics before repair")
    await run_compose(policy, ["start", "--wait", "--wait-timeout", "30"])
    result = await probe_connection()
    if not result["reachable"]:
        raise RuntimeError("Redis service start did not restore connectivity: " + result["error"])
    return {**result, "action": "started_existing_service"}
