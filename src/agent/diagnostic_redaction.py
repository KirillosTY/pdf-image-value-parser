"""Remove credentials from bounded startup observations and checkpointed errors."""

import os
import re
from urllib.parse import unquote, urlsplit


def redact_diagnostic(value, *, limit=4000):
    """Keep useful failure evidence without exposing endpoint or service secrets."""
    text = str(value)
    secrets = set()
    for name, content in os.environ.items():
        if re.search(r"PASSWORD|SECRET|TOKEN|API_KEY", name, re.IGNORECASE):
            secrets.add(content)
        if "://" in content:
            try:
                password = urlsplit(content).password
                if password:
                    secrets.update([password, unquote(password)])
            except ValueError:
                pass
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    text = re.sub(r"(\w+://)[^\s/@]+@", r"\1[redacted]@", text)
    text = re.sub(r"(?i)((?:password|token|secret|api_key)\s*[=:]\s*)[^\s,;]+",
                  r"\1[redacted]", text)
    return text[-limit:]


def redact_observation(value):
    """Preserve structured tool results while redacting each textual value."""
    if isinstance(value, dict):
        return {key: redact_observation(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_observation(item) for item in value]
    if isinstance(value, str):
        return redact_diagnostic(value)
    return value
