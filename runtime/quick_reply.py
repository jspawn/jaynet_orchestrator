"""Quick-reply fast-path for trivial messages (greetings, thanks, bye).

Pattern-matches the raw user message against config/quick-replies.yaml.
On a hit, returns an instant personalized response without touching the
brain model — zero prefill, zero GPU, <50ms.

Usage:
    from runtime.quick_reply import QuickReply
    qr = QuickReply(config_path)        # load once at startup
    reply = qr.match(message, username)  # None if no match
"""

from __future__ import annotations

import random
import re
from pathlib import Path

import yaml


class QuickReply:
    def __init__(self, config_path: str | Path | None = None):
        self.rules: list[tuple[re.Pattern, list[str]]] = []
        if config_path is None:
            from runtime.paths import HOME
            config_path = HOME / "config" / "quick-replies.yaml"
        path = Path(config_path)
        if not path.exists():
            return
        try:
            entries = yaml.safe_load(path.read_text()) or []
        except Exception:
            return
        for entry in entries:
            pat = entry.get("pattern")
            resps = entry.get("responses", [])
            if pat and resps:
                try:
                    self.rules.append((re.compile(pat, re.IGNORECASE), resps))
                except re.error:
                    pass

    def match(self, message: str, username: str = "there") -> str | None:
        """Return a personalized response if the message matches a pattern, else None."""
        text = (message or "").strip()
        # Skip if there are attachments indicators or the message is too long
        if len(text) > 80:
            return None
        for pattern, responses in self.rules:
            if pattern.match(text):
                reply = random.choice(responses)
                name = _display_name(username)
                return reply.format(name=name, time=_time_greeting())
        return None

    def __len__(self) -> int:
        return len(self.rules)


def _display_name(username: str) -> str:
    """Turn a username into a friendly display name."""
    if not username or username in ("_token", "anonymous"):
        return "there"
    # Capitalize first letter of each word, handle dots/underscores
    parts = username.replace("_", " ").replace(".", " ").title().split()
    if not parts:
        # A handle that normalizes to pure whitespace (e.g. "___") has no word to
        # lead with — fall back to the raw name, or the neutral default.
        return username.strip() or "there"
    return parts[0]


def _time_greeting() -> str:
    """Return a time-of-day word based on current hour."""
    from datetime import datetime
    h = datetime.now().hour
    if h < 12:
        return "morning"
    elif h < 17:
        return "afternoon"
    else:
        return "evening"
