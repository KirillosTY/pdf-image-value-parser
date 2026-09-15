"""Create Redis clients using the same connection settings."""

import os

from redis import Redis
from redis.asyncio import Redis as AsyncRedis

DEFAULT_REDIS_URL = "redis://localhost:6379/0"


def redis_url() -> str:
    """Read the configured URL without exposing it in graph state."""
    return os.getenv("REDIS_URL", DEFAULT_REDIS_URL)


def redis_client() -> Redis:
    """Create a client for synchronous extraction workers."""
    return Redis.from_url(
        redis_url(),
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=10,
    )


def async_redis_client() -> AsyncRedis:
    """Create a client for asynchronous graph operations."""
    return AsyncRedis.from_url(
        redis_url(),
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=10,
    )
