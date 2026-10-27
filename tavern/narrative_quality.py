from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


_CONTROL_PATTERNS = (
    re.compile(r"(?:你|玩家角色)(?:决定|答应|拒绝|认为|感到|意识到|毫不犹豫)"),
    re.compile(r"(?:替|代)(?:你|玩家)(?:作出|决定|选择)"),
)


def inspect_narrative(
    narrative: str,
    choices: Sequence[Mapping[str, Any]] | None = None,
    *,
    acting_name: str = "",
    previous_narrative: str = "",
) -> dict[str, Any]:
    """Fast, deterministic checks before a model result reaches commit."""

    text = str(narrative or "").strip()
    findings: list[dict[str, Any]] = []
    if not text:
        findings.append(
            {"level": "error", "code": "empty_narrative", "message": "故事正文为空"}
        )
    for pattern in _CONTROL_PATTERNS:
        if pattern.search(text):
            findings.append(
                {
                    "level": "warning",
                    "code": "possible_player_control",
                    "message": "正文可能替玩家决定行动、情绪或立场",
                }
            )
            break
    previous = str(previous_narrative or "").strip()
    if previous and len(text) >= 80 and text[:80] == previous[:80]:
        findings.append(
            {
                "level": "warning",
                "code": "repeated_opening",
                "message": "正文开头与上一轮高度重复",
            }
        )
    option_texts = [
        str(item.get("text") or "").strip()
        for item in (choices or [])
        if isinstance(item, Mapping)
    ]
    normalized = {re.sub(r"\W+", "", item) for item in option_texts if item}
    if option_texts and len(normalized) != len(option_texts):
        findings.append(
            {
                "level": "error",
                "code": "duplicate_choices",
                "message": "存在实质重复的行动选项",
            }
        )
    if acting_name and choices:
        wrong_actor = [
            str(item.get("actor_id") or "")
            for item in choices
            if item.get("actor_id")
            and str(item.get("actor_id")) not in {acting_name, "current"}
        ]
        if wrong_actor:
            findings.append(
                {
                    "level": "warning",
                    "code": "actor_mismatch",
                    "message": "部分选项声明了与当前行动者不一致的 actor_id",
                }
            )
    return {
        "passed": not any(item["level"] == "error" for item in findings),
        "score": max(0, 100 - 30 * sum(x["level"] == "error" for x in findings) - 10 * sum(x["level"] == "warning" for x in findings)),
        "findings": findings,
    }

