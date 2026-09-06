"""建卡 AI 设定生成服务（平台无关）。

为 ``/团 随机`` 与 ``/团 补全`` 两条建卡指令提供提示词构建与生成编排：

- 输入：私聊建卡草稿映射（``card_draft_for_private`` 返回结构）；
- 输出：可直接交给 ``fill_card_draft`` 的字段值——文本字段是设定正文，
  预设字段是命中的候选标签；落库仍走标准字段校验与游标推进流程；
- 语言模型经注入的异步回调调用，本模块不 import AstrBot 宿主类型，
  也不执行平台发送。

生成正文可能超出字段的字数上限，本模块会先在句子边界截断，
避免填写阶段因超长而失败；模型漏答候选名称时统一抛出
:class:`CardAIError`，由调用层转成玩家可见文案。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from .card_wizard import (
    PRESET_REFS_KEY,
    preset_options,
    resolve_current_wizard_step,
)
from .lifecycle.character_creation import staged_creation
from .lifecycle.world_time import CARD_STAGE_A
from .security import truncate_text

logger = logging.getLogger(__name__)

# 单次生成超时与输出预算：设定正文不需要长篇输出，超时即判失败。
AI_GENERATE_TIMEOUT_SECONDS = 120.0
AI_MAX_TOKENS = 900
AI_RANDOM_TEMPERATURE = 0.9
AI_EXPAND_TEMPERATURE = 0.7

_TEXT_FIELD_TYPES = frozenset({"text", "textarea"})
_OPTION_FIELD_TYPES = frozenset({"select", "preset_select"})
_NAME_ROLES = frozenset({"actor.identity.name", "actor.identity.alias"})

_MAX_CONTEXT_ENTRIES = 10
_MAX_CONTEXT_VALUE_CHARS = 120
_MAX_OPTION_ENTRIES = 60
_MAX_OPTION_DESC_CHARS = 80
# 玩家初稿进入提示词前的长度上限：补全只需要核心创意，超长粘贴没有收益。
_MAX_USER_DRAFT_CHARS = 800
# 句子边界截断时允许的最短保留长度，避免截出无意义的半句。
_MIN_SENTENCE_CUT = 40

AI_SYSTEM_PROMPT = (
    "你是桌游角色卡的设定撰写助手，负责为玩家的角色卡草稿撰写单个字段的设定。\n"
    "写作要求：\n"
    "1. 使用简体中文，风格与角色已有资料保持一致，符合世界观基调。\n"
    "2. 只输出该字段的设定正文本身；不要输出标题、序号、Markdown 代码块、"
    "引号包裹或任何解释说明。\n"
    "3. 设定要具体、可演出：交代来历、外观、特性等细节；凡涉及特殊能力，"
    "效果与代价应当对等，避免无代价的纯增益。\n"
    "4. 不得引入超出角色既定能力与世界观规则的设定。\n"
    "5. 严格遵守给出的字数上限。"
)

# 常见的模型作答前缀；输出清洗时按序剥离。
_LEADING_PREFIXES = ("设定：", "设定:", "正文：", "正文:", "内容：", "内容:")


class CardAIError(RuntimeError):
    """建卡 AI 生成的已预期失败；消息直接面向玩家。"""


async def _noop_generate(*args: Any, **kwargs: Any) -> str:
    raise CardAIError("AI 设定助手未启用。")


class CardAIComposer:
    """把当前建卡字段交给语言模型生成，产出可直接填写的值。

    ``generate`` 是入口层注入的异步回调，签名为
    ``(origin, prompt, system_prompt, *, max_tokens, temperature) -> str``，
    由它负责挑选可用模型并调用宿主 LLM 接口；本类只做提示词编排、
    输出清洗与同一私聊的并发防护。
    """

    def __init__(
        self,
        generate: Callable[..., Awaitable[str]] | None = None,
    ) -> None:
        self._generate = generate or _noop_generate
        self._inflight: set[str] = set()

    async def compose_field_value(
        self,
        origin: str,
        draft: Mapping[str, Any],
        *,
        mode: str,
        user_draft: str = "",
    ) -> tuple[str, str, str]:
        """生成当前字段的填写值。

        返回 ``(填写值, 字段名, 外显正文)``：文本字段的填写值与外显正文
        一致；预设字段的填写值是命中的候选标签。``mode`` 仅支持
        ``random``（随机生成）与 ``expand``（按玩家初稿补全）。
        """

        key = str(origin or "")
        if key in self._inflight:
            raise CardAIError("上一条设定还在生成中，请等它结束后再试。")
        self._inflight.add(key)
        try:
            return await self._compose(
                key,
                draft,
                mode=mode,
                user_draft=user_draft,
            )
        finally:
            self._inflight.discard(key)

    async def _compose(
        self,
        origin: str,
        draft: Mapping[str, Any],
        *,
        mode: str,
        user_draft: str,
    ) -> tuple[str, str, str]:
        template = draft.get("template")
        template = template if isinstance(template, Mapping) else {}
        fields = draft.get("fields")
        fields = fields if isinstance(fields, Mapping) else {}
        step = int(draft.get("current_step", draft.get("draft_step", 0)) or 0)
        allow_stages = (CARD_STAGE_A,) if staged_creation(template) else None
        try:
            wizard = resolve_current_wizard_step(
                template,
                fields,
                step,
                allow_stages=allow_stages,
            )
        except (TypeError, ValueError) as exc:
            raise CardAIError(
                "当前字段无法解析，无法安全生成设定；"
                "请联系主持人检查世界包。"
            ) from exc
        if wizard is None:
            raise CardAIError(
                "角色卡必填资料已填写完成；如需修改请发送 /团 修改 <字段名称>，"
                "确认无误后发送 /团 确认建卡。"
            )
        if wizard.kind == "synthetic":
            raise CardAIError(
                f"当前步骤是「{wizard.label}」，需要玩家亲自选择建卡方式，"
                "AI 不代作决定；请直接回复候选序号。"
            )
        if not wizard.user_fillable or wizard.auto_filled:
            raise CardAIError(
                f"当前字段「{wizard.label}」由系统代填，无法使用 AI 生成。"
            )
        definition = wizard.definition
        field_type = wizard.field_type
        label = wizard.label
        if field_type == "integer":
            raise CardAIError(
                f"当前字段「{label}」是数值分配，需要按点数预算手动填写；"
                "请直接回复数值。"
            )
        world_name = _world_display_name(draft, template)
        context_lines = _character_context(template, fields)
        if field_type in _TEXT_FIELD_TYPES:
            return await self._compose_prose(
                origin,
                definition,
                label=label,
                mode=mode,
                user_draft=user_draft,
                world_name=world_name,
                context_lines=context_lines,
            )
        if field_type in _OPTION_FIELD_TYPES or field_type == "multi_select":
            return await self._compose_options(
                origin,
                template,
                definition,
                fields,
                label=label,
                mode=mode,
                user_draft=user_draft,
                world_name=world_name,
                context_lines=context_lines,
                multi=field_type == "multi_select",
            )
        raise CardAIError(
            f"当前字段「{label}」使用了暂不支持自动生成的类型；请手动填写。"
        )

    async def _compose_prose(
        self,
        origin: str,
        definition: Mapping[str, Any],
        *,
        label: str,
        mode: str,
        user_draft: str,
        world_name: str,
        context_lines: list[str],
    ) -> tuple[str, str, str]:
        max_chars = int(definition.get("max_chars", 0) or 0)
        if max_chars <= 0:
            raise CardAIError(
                f"字段「{label}」缺少有效的字数上限配置，无法安全生成；"
                "请手动填写或联系管理员修复世界包。"
            )
        description = str(definition.get("description") or "").strip()
        user_draft = truncate_text(
            user_draft,
            max_chars=_MAX_USER_DRAFT_CHARS,
        )
        prompt = _prose_prompt(
            mode=mode,
            label=label,
            description=description,
            max_chars=max_chars,
            world_name=world_name,
            context_lines=context_lines,
            user_draft=user_draft,
        )
        temperature = (
            AI_RANDOM_TEMPERATURE if mode == "random" else AI_EXPAND_TEMPERATURE
        )
        raw = await self._call(
            origin,
            prompt,
            max_tokens=AI_MAX_TOKENS,
            temperature=temperature,
        )
        value = _trim_prose(raw, max_chars)
        if not value:
            raise CardAIError("模型没有返回有效内容，请重试一次。")
        return value, label, value

    async def _compose_options(
        self,
        origin: str,
        template: Mapping[str, Any],
        definition: Mapping[str, Any],
        fields: Mapping[str, Any],
        *,
        label: str,
        mode: str,
        user_draft: str,
        world_name: str,
        context_lines: list[str],
        multi: bool,
    ) -> tuple[str, str, str]:
        try:
            options = preset_options(template, definition, fields)
        except ValueError as exc:
            raise CardAIError(str(exc)) from exc
        if not options:
            raise CardAIError(
                f"字段「{label}」当前没有可用候选，无法生成选择；"
                "请联系主持人检查世界包。"
            )
        minimum = (
            max(0, int(definition.get("min_choices", 0) or 0)) if multi else 1
        )
        maximum = (
            max(minimum, int(definition.get("max_choices", 100) or 100))
            if multi
            else 1
        )
        prompt = _option_prompt(
            mode=mode,
            label=label,
            description=str(definition.get("description") or "").strip(),
            options=options,
            world_name=world_name,
            context_lines=context_lines,
            user_draft=truncate_text(
                user_draft,
                max_chars=_MAX_USER_DRAFT_CHARS,
            ),
            multi=multi,
            minimum=minimum,
            maximum=maximum,
        )
        temperature = 0.5 if mode == "random" else 0.2
        raw = await self._call(
            origin,
            prompt,
            max_tokens=200,
            temperature=temperature,
        )
        picked = _pick_options(
            raw,
            options,
            label=label,
            minimum=minimum,
            maximum=maximum,
            multi=multi,
        )
        value = "、".join(
            str(item.get("label") or item.get("value") or "") for item in picked
        )
        return value, label, value

    async def _call(
        self,
        origin: str,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
    ) -> str:
        try:
            raw = await asyncio.wait_for(
                self._generate(
                    origin,
                    prompt,
                    AI_SYSTEM_PROMPT,
                    max_tokens=max_tokens,
                    temperature=temperature,
                ),
                timeout=AI_GENERATE_TIMEOUT_SECONDS,
            )
        except CardAIError:
            raise
        except asyncio.TimeoutError as exc:
            logger.warning("321开团建卡 AI 生成超时")
            raise CardAIError("模型生成超时，请稍后重试。") from exc
        except Exception as exc:
            logger.warning("321开团建卡 AI 生成失败", exc_info=True)
            raise CardAIError(
                "模型调用失败，请稍后重试；若持续失败请联系管理员检查模型配置。"
            ) from exc
        return str(raw or "")


def _world_display_name(
    draft: Mapping[str, Any],
    template: Mapping[str, Any],
) -> str:
    world = draft.get("world")
    world = world if isinstance(world, Mapping) else {}
    for source, keys in (
        (world, ("name", "display_name", "title", "world_name")),
        (template, ("world_name", "title", "name")),
    ):
        for key in keys:
            text = str(source.get(key) or "").strip()
            if text:
                return text
    return ""


def _character_context(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
) -> list[str]:
    """把已填字段压缩成提示词上下文行（预设字段优先使用其外显标签）。"""

    refs = fields.get(PRESET_REFS_KEY)
    refs = refs if isinstance(refs, Mapping) else {}
    lines: list[str] = []
    for item in template.get("fields") or []:
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("key") or "")
        if not key or key.startswith("_"):
            continue
        value = fields.get(key)
        if value in (None, ""):
            continue
        ref = refs.get(key)
        if isinstance(ref, Mapping) and ref.get("label"):
            text = str(ref["label"]).strip()
        elif isinstance(ref, Sequence) and not isinstance(ref, (str, bytes)):
            text = "、".join(
                str(part.get("label") or part.get("value") or "").strip()
                for part in ref
                if isinstance(part, Mapping)
            ).strip("、")
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            text = "、".join(str(part) for part in value)
        else:
            text = str(value).strip()
        if not text:
            continue
        if len(text) > _MAX_CONTEXT_VALUE_CHARS:
            text = text[:_MAX_CONTEXT_VALUE_CHARS] + "…"
        lines.append(f"- {item.get('label') or key}：{text}")
        if len(lines) >= _MAX_CONTEXT_ENTRIES:
            break
    return lines


def _prose_prompt(
    *,
    mode: str,
    label: str,
    description: str,
    max_chars: int,
    world_name: str,
    context_lines: list[str],
    user_draft: str,
) -> str:
    parts: list[str] = []
    if world_name:
        parts.append(f"世界观：{world_name}")
    if context_lines:
        parts.append("角色已有资料：\n" + "\n".join(context_lines))
    parts.append(f"当前要填写的字段：「{label}」")
    if description:
        parts.append(f"字段说明：{description}")
    parts.append(f"字数上限：不超过 {max_chars} 字（含标点）。")
    if mode == "expand":
        parts.append(f"玩家初稿：{user_draft}")
        parts.append(
            "请在完整保留玩家初稿的核心创意与关键词的前提下，"
            "把这份设定扩写得更完整：补充来历、外观、特性、效果与代价等细节，"
            "使内容可以直接用于角色卡；风格须与初稿一致。"
            "直接输出扩写后的设定正文。"
        )
    else:
        parts.append(
            "请为该字段随机创造一份全新的设定，"
            "并与角色已有资料保持协调。直接输出设定正文。"
        )
    return "\n\n".join(parts)


def _option_prompt(
    *,
    mode: str,
    label: str,
    description: str,
    options: Sequence[Mapping[str, Any]],
    world_name: str,
    context_lines: list[str],
    user_draft: str,
    multi: bool,
    minimum: int,
    maximum: int,
) -> str:
    parts: list[str] = []
    if world_name:
        parts.append(f"世界观：{world_name}")
    if context_lines:
        parts.append("角色已有资料：\n" + "\n".join(context_lines))
    parts.append(
        f"当前要填写的字段：「{label}」，需要从下方候选中"
        + ("选出若干项" if multi else "选出一项")
        + "。"
    )
    if description:
        parts.append(f"字段说明：{description}")
    lines = []
    for index, option in enumerate(options[:_MAX_OPTION_ENTRIES], 1):
        name = str(option.get("label") or option.get("value") or "").strip()
        brief = str(option.get("description") or "").strip()
        if len(brief) > _MAX_OPTION_DESC_CHARS:
            brief = brief[:_MAX_OPTION_DESC_CHARS] + "…"
        lines.append(f"{index}. {name}" + (f" —— {brief}" if brief else ""))
    if len(options) > _MAX_OPTION_ENTRIES:
        lines.append(f"（候选较多，仅列出前 {_MAX_OPTION_ENTRIES} 项）")
    parts.append("候选列表：\n" + "\n".join(lines))
    if multi:
        expected = f"{minimum} 项" if minimum == maximum else f"{minimum}—{maximum} 项"
        parts.append(
            f"请结合角色已有资料选出最合适的 {expected} 候选"
            "（以顿号、逗号或空格分隔），只输出候选的完整名称，"
            "不要输出序号或任何解释。"
        )
    elif mode == "expand":
        parts.append(f"玩家描述：{user_draft}")
        parts.append(
            "请从候选中选出与玩家描述最匹配的一项，"
            "只输出该候选的完整名称，不要输出序号或任何解释。"
        )
    else:
        parts.append(
            "请结合角色已有资料，选出最契合的一项候选；"
            "只输出该候选的完整名称，不要输出序号或任何解释。"
        )
    return "\n\n".join(parts)


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[^\n]*\n?", "", cleaned)
        cleaned = re.sub(r"```\s*$", "", cleaned)
    return cleaned.strip()


def _trim_prose(text: str, max_chars: int) -> str:
    """清洗模型正文：剥离包裹符与作答前缀，并按句子边界截到上限内。

    前缀与成对包裹符可能嵌套出现（如 ``“设定：正文”``），因此按
    ``前缀 → 包裹符`` 的顺序多轮清洗，直到内容稳定。
    """

    cleaned = _strip_fences(text)
    for _ in range(2):
        changed = False
        for prefix in _LEADING_PREFIXES:
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
                changed = True
        if (
            len(cleaned) >= 2
            and cleaned[0] in "\"“”「」『』'"
            and cleaned[-1] in "\"“”「」『』'"
        ):
            cleaned = cleaned[1:-1].strip()
            changed = True
        if not changed:
            break
    if not cleaned:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned
    cut = cleaned[:max_chars]
    for separator in ("。", "！", "？", "；", "!", "?", ";", "\n"):
        position = cut.rfind(separator)
        if position >= _MIN_SENTENCE_CUT:
            return cut[: position + 1]
    return cut


def _option_identity(option: Mapping[str, Any]) -> set[str]:
    identities = {
        str(option.get(key) or "").strip().casefold()
        for key in ("id", "value", "label", "name")
    }
    identities.update(
        str(item).strip().casefold()
        for item in (option.get("aliases") or [])
        if str(item).strip()
    )
    identities.discard("")
    return identities


def _pick_options(
    raw: str,
    options: Sequence[Mapping[str, Any]],
    *,
    label: str,
    minimum: int,
    maximum: int,
    multi: bool,
) -> list[Mapping[str, Any]]:
    """把模型作答解析回候选列表；无法唯一命中时抛出 CardAIError。"""

    cleaned = _strip_fences(raw)
    tokens = [
        token.strip(" \t\"“”'「」『』.。;；!！?？")
        for token in re.split(r"[,，、;；\n]+", cleaned)
        if token.strip(" \t\"“”'「」『』.。;；!！?？")
    ]
    picked: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for token in tokens[:maximum] if multi else tokens[:1]:
        match = _match_single_option(token, options)
        if match is None:
            continue
        identity = str(match.get("id") or match.get("label") or "")
        if identity in seen:
            continue
        seen.add(identity)
        picked.append(match)
    if not picked:
        raise CardAIError(
            f"模型回复无法匹配「{label}」的候选选项，请重试一次，"
            "或直接回复候选序号手动选择。"
        )
    if len(picked) < minimum or len(picked) > maximum:
        expected = (
            f"{minimum} 项" if minimum == maximum else f"{minimum}—{maximum} 项"
        )
        raise CardAIError(
            f"「{label}」需要选择 {expected}，模型只给出 {len(picked)} 项；"
            "请重试一次，或直接回复候选序号手动选择。"
        )
    return picked


def _match_single_option(
    token: str,
    options: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    text = token.strip()
    if not text:
        return None
    for prefix in _LEADING_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    if text.isdigit():
        ordinal = int(text)
        if 1 <= ordinal <= len(options):
            return options[ordinal - 1]
        return None
    reference = text.casefold()
    matches = [
        option
        for option in options
        if reference in _option_identity(option)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        # 模型偶尔会复述候选全称或附加修饰语；退化为唯一包含匹配。
        matches = [
            option
            for option in options
            if _unique_containment(reference, option)
        ]
    return matches[0] if len(matches) == 1 else None


def _unique_containment(reference: str, option: Mapping[str, Any]) -> bool:
    if len(reference) < 2:
        return False
    return any(
        reference in name or name in reference
        for name in _option_identity(option)
    )


__all__ = [
    "AI_GENERATE_TIMEOUT_SECONDS",
    "AI_MAX_TOKENS",
    "CardAIComposer",
    "CardAIError",
]
