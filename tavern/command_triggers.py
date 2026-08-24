"""Normalization, matching, and display helpers for command triggers."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any


DEFAULT_COMMAND_TRIGGER = "酒馆"
_CANONICAL_COMMAND_PATTERN = re.compile(r"/酒馆(?=\s|$)")


def normalize_command_triggers(
    raw: Any,
    *,
    story_trigger: str,
) -> tuple[str, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return (DEFAULT_COMMAND_TRIGGER,)
    story_key = str(story_trigger or "").strip().casefold()
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = "" if item is None else str(item).strip()
        if text.startswith(("/", "／")):
            text = text[1:].strip()
        key = text.casefold()
        if (
            not text
            or len(text) > 16
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in text)
            or key == story_key
            or key in seen
        ):
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= 8:
            break
    return tuple(result) or (DEFAULT_COMMAND_TRIGGER,)


def canonicalize_command_message(
    message: str,
    triggers: Sequence[str],
    *,
    allow_bare: bool,
) -> str | None:
    text = str(message or "").strip()
    if not text:
        return None
    has_slash = text.startswith(("/", "／"))
    if has_slash:
        body = text[1:]
    elif allow_bare:
        body = text
    else:
        return None
    for trigger in triggers:
        candidate = str(trigger or "")
        if not candidate:
            continue
        if body[: len(candidate)].casefold() != candidate.casefold():
            continue
        if len(body) > len(candidate) and not body[len(candidate)].isspace():
            continue
        rest = body[len(candidate) :].strip()
        return "/酒馆" + (f" {rest}" if rest else "")
    return None


def render_command_text(text: Any, primary_trigger: str) -> str:
    value = str(text or "")
    trigger = str(primary_trigger or DEFAULT_COMMAND_TRIGGER).strip()
    return _CANONICAL_COMMAND_PATTERN.sub(lambda _match: f"/{trigger}", value)


__all__ = [
    "canonicalize_command_message",
    "normalize_command_triggers",
    "render_command_text",
]
