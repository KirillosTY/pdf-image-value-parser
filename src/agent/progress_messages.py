"""Print brief action summaries when the saved run enables show_thinking."""

from datetime import datetime

from agent.diagnostic_redaction import redact_diagnostic

STARTUP_ACTIONS = {
    "set_vlm_routes": "set the default image-to-VLM routes",
    "set_formatter_routes": "set the default formatter routes",
    "ensure_redis": "check the Redis connection",
    "create_vlm_queues": "create the VLM queues",
    "create_formatter_queues": "create the formatter queues",
    "schedule_models": "schedule the models within the available memory",
    "start_docling": "hand the documents to Docling",
    "diagnose_redis": "diagnose the Redis problem",
    "start_redis_service": "start the configured Redis service",
    "inspect_startup_resources": "check available RAM and GPU memory",
    "abort_startup": "stop startup",
}


def show_progress(config, message):
    """Flush one bounded, redacted status line; never print model reasoning or payloads."""
    if not config.get("show_thinking", False):
        return
    text = " ".join(redact_diagnostic(message).split())[:500]
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {text}", flush=True)  # noqa: T201


def startup_action(name):
    """Describe a known startup tool without echoing generated arguments."""
    return STARTUP_ACTIONS.get(name, "handle a startup tool request")
