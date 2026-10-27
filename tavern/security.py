from __future__ import annotations

import re
import time
from dataclasses import dataclass
from threading import Lock

from .constants import MANAGEMENT_ACTIONS

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_COMMAND = re.compile(r"^\s*[/／!！]酒馆(?:\s+|$)(.*)$", re.DOTALL)
_SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def clean_text(value: object, *, max_chars: int) -> str:
    text = _CONTROL_CHARS.sub("", str(value or "")).strip()
    if len(text) > max_chars:
        raise ValueError(f"内容超过 {max_chars} 字符上限")
    return text


def truncate_text(value: object, *, max_chars: int) -> str:
    """Strip control characters and truncate without raising (A15).

    Unlike :func:`clean_text`, over-long values are silently cut at
    ``max_chars`` instead of raising. Used where the length of a value
    is not a hard protocol error (e.g. collecting trusted bonus sources
    from free-form character card fields).
    """
    text = _CONTROL_CHARS.sub("", str(value or "")).strip()
    if len(text) > max_chars:
        return text[:max_chars]
    return text


def validate_slug(value: object) -> str:
    slug = str(value or "").strip().lower()
    if not _SAFE_SLUG.fullmatch(slug):
        raise ValueError("标识仅允许小写字母、数字、下划线和连字符")
    return slug


def validate_platform_id(value: object, *, label: str = "ID") -> str:
    text = str(value or "").strip()
    if not text or len(text) > 128:
        raise ValueError(f"{label} 不能为空且不得超过 128 字符")
    if _CONTROL_CHARS.search(text):
        raise ValueError(f"{label} 含非法控制字符")
    return text


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    matched: bool
    action: str = ""
    argument: str = ""
    raw_action: str = ""


def parse_tavern_command(message: str) -> ParsedCommand:
    match = _COMMAND.match(message or "")
    if not match:
        return ParsedCommand(matched=False)
    body = match.group(1).strip()
    if not body:
        return ParsedCommand(matched=True, action="help")
    parts = body.split(maxsplit=1)
    action_text = parts[0]
    argument = parts[1] if len(parts) > 1 else ""
    action = MANAGEMENT_ACTIONS.get(action_text.strip(), "unknown")
    return ParsedCommand(
        matched=True,
        action=action,
        argument=argument.strip(),
        raw_action=action_text.strip(),
    )


def parse_story_trigger(message: str, prefix: str) -> str | None:
    """Return content only for ``<prefix><whitespace><content>``.

    Leading text, a bare prefix, and visually similar longer words do not
    trigger the story engine.
    """

    text = str(message or "")
    trigger = str(prefix or "").strip()
    if not trigger or len(text) <= len(trigger):
        return None
    if text[: len(trigger)].casefold() != trigger.casefold():
        return None
    if not text[len(trigger)].isspace():
        return None
    content = text[len(trigger) :].lstrip()
    return content or None


class RateLimiter:
    """In-memory cooldown. It is advisory; the per-session lock is authoritative."""

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], float] = {}
        self._lock = Lock()

    def remaining(
        self,
        session_id: str,
        sender_id: str,
        cooldown_seconds: float,
    ) -> float:
        if cooldown_seconds <= 0:
            return 0.0
        key = (str(session_id), str(sender_id))
        now = time.monotonic()
        with self._lock:
            previous = self._values.get(key, 0.0)
            remaining = cooldown_seconds - (now - previous)
            if remaining <= 0:
                self._values[key] = now
                return 0.0
            return remaining

    def clear_session(self, session_id: str) -> None:
        with self._lock:
            doomed = [key for key in self._values if key[0] == session_id]
            for key in doomed:
                self._values.pop(key, None)
