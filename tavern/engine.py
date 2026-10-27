from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from .config import TavernConfig
from .database import (
    DatabaseConflictError,
    InvalidTransitionError,
    TavernDatabase,
)
from .events import EventBroker
from .api.registry import ExtensionRegistry
from .lifecycle import (
    fallback_choices,
    format_choices,
    normalize_choices_compat,
)
from .prompts import (
    checked_resolution_prompt,
    choice_generation_prompt,
    choice_repair_prompt,
    choice_system_prompt,
    dm_beat_prompt,
    planning_prompt,
    repair_prompt,
    system_prompt,
)
from .resolution import (
    CheckRequest,
    DiceResult,
    Resolution,
    apply_state_patch,
    extract_json_object,
    roll_check,
    roll_group_check,
    roll_opposed_check,
    validate_resolution,
)
from .entity_resolver import (
    build_participant_labels,
    normalize_relationship_ops,
)
from .security import RateLimiter, clean_text
from .world_contract import world_contract
from .operations import operation_key, transport_event_id
from .narrative_quality import inspect_narrative
from .chat_experience import normalize_chat_experience


logger = logging.getLogger(__name__)


def _owner_tuple(owner_type: Any, owner_ref: Any) -> tuple[str, str] | None:
    ot = str(owner_type or "").strip()
    ore = str(owner_ref or "").strip()
    if not ot or not ore:
        return None
    return (ot, ore)

# 0.11.1：单次结构化生成（检定/选项/直述）的全局模型调用上限。
# 默认 json_repair_attempts=1 时正常路径仅 1-2 次调用；此上限用于兜底
# 多 provider × 多 repair 叠加造成分钟级延迟的场景。
_MAX_TOTAL_MODEL_ATTEMPTS = 8


def _builtin_d20_provider(
    *,
    check: CheckRequest,
    check_type: str,
    actors: list[Mapping[str, Any]] | None = None,
    outcome_policy: Mapping[str, Any] | None = None,
) -> DiceResult:
    if check_type in {"group", "resistance"}:
        return roll_group_check(check, list(actors or []), outcome_policy)
    if check_type == "opposed":
        return roll_opposed_check(check, outcome_policy=outcome_policy)
    return roll_check(check, outcome_policy)


class TavernEngineError(RuntimeError):
    pass


class TavernBusyError(TavernEngineError):
    pass


class TavernPlayerDisabledError(TavernEngineError):
    pass


class TavernTurnOrderError(TavernEngineError):
    def __init__(
        self,
        message: str,
        *,
        turn: Mapping[str, Any],
        joined: bool = False,
    ) -> None:
        super().__init__(message)
        self.turn = dict(turn)
        self.joined = bool(joined)


@dataclass(frozen=True, slots=True)
class EngineReply:
    text: str
    session: dict[str, Any]
    dice: DiceResult | None = None
    ooc: bool = False
    turn: dict[str, Any] | None = None
    story_text: str = ""
    turn_text: str = ""


class TavernEngine:
    def __init__(
        self,
        *,
        context: Any,
        database: TavernDatabase,
        config_provider: Callable[[], TavernConfig],
        broker: EventBroker,
        extensions: ExtensionRegistry | None = None,
    ) -> None:
        self.context = context
        self.database = database
        self.config_provider = config_provider
        self.broker = broker
        self.extensions = extensions or ExtensionRegistry()
        if self.extensions.resolve("dice_system", "d20") is None:
            self.extensions.register_dice_system("d20", _builtin_d20_provider)
        self.rate_limiter = RateLimiter()
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    @staticmethod
    async def _emit_progress(
        callback: Callable[[str], Any] | None,
        message: str,
    ) -> None:
        if callback is None:
            return
        result = callback(message)
        if inspect.isawaitable(result):
            await result

    async def _roll_with_registered_system(
        self,
        world: Mapping[str, Any],
        check: CheckRequest,
        *,
        actors: list[Mapping[str, Any]] | None = None,
    ) -> DiceResult:
        contract = world_contract(world)
        system_name = str(
            contract["resolution"].get("dice_system") or ""
        ).strip().lower()
        provider = self.extensions.resolve("dice_system", system_name)
        if provider is None:
            raise TavernEngineError(
                f"世界要求骰制“{system_name}”，但运行时没有注册该骰制"
            )
        result = provider(
            check=check,
            check_type=check.check_type,
            actors=list(actors or []),
            outcome_policy=contract["resolution"].get("outcome_policy") or {},
        )
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, DiceResult):
            raise TavernEngineError(
                f"骰制“{system_name}”没有返回合法 DiceResult"
            )
        return result

    def validate_world_runtime(self, world: Mapping[str, Any]) -> None:
        contract = world_contract(world)
        if contract["resolution"]["mode"] not in {"dice_only", "attribute"}:
            return
        system_name = str(
            contract["resolution"].get("dice_system") or ""
        ).strip().lower()
        if self.extensions.resolve("dice_system", system_name) is None:
            raise TavernEngineError(
                f"世界要求骰制“{system_name}”，但运行时没有注册该骰制"
            )

    async def _session_lock(self, session_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock

    async def release_session_lock(self, session_id: str) -> None:
        """0.11.1：副本关闭/完结/删除后回收会话锁，避免 _locks 无限增长。"""
        async with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is not None and not lock.locked():
                self._locks.pop(session_id, None)

    @staticmethod
    def _usage_value(source: Any, *names: str) -> int:
        for name in names:
            if isinstance(source, Mapping):
                value = source.get(name)
            else:
                value = getattr(source, name, None)
            try:
                if value is not None:
                    return max(0, int(value))
            except (TypeError, ValueError, OverflowError):
                continue
        return 0

    @classmethod
    def _response_usage(
        cls,
        response: Any,
        *,
        prompt: str,
        system: str,
    ) -> tuple[int, int, int, str]:
        usage = (
            getattr(response, "usage", None)
            or getattr(response, "token_usage", None)
            or getattr(response, "usage_metadata", None)
        )
        input_tokens = cls._usage_value(
            usage,
            "input_tokens",
            "prompt_tokens",
            "input",
            "input_other",
        )
        cached = cls._usage_value(
            usage,
            "cached_input_tokens",
            "input_cached_tokens",
            "cache_read_input_tokens",
            "input_cached",
        )
        output_tokens = cls._usage_value(
            usage,
            "output_tokens",
            "completion_tokens",
            "output",
        )
        if input_tokens or output_tokens:
            return input_tokens, cached, output_tokens, "provider"
        completion = str(
            getattr(response, "completion_text", "") or ""
        )
        estimated_input = max(
            1,
            (len(str(prompt or "")) + len(str(system or "")) + 1) // 2,
        )
        estimated_output = max(1, (len(completion) + 1) // 2)
        return estimated_input, 0, estimated_output, "estimated"

    async def _llm_generate_metered(
        self,
        *,
        session_id: str,
        request_type: str,
        provider_id: str,
        prompt: str,
        system_prompt_value: str = "",
        max_tokens: int,
        **kwargs: Any,
    ) -> Any:
        expected_input = max(
            1,
            len(str(prompt or ""))
            + len(str(system_prompt_value or "")),
        )
        reservation = await self.database.reserve_token_usage(
            session_id,
            request_type,
            provider_id,
            expected_input + max(1, int(max_tokens)),
        )
        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt_value or None,
                max_tokens=max_tokens,
                **kwargs,
            )
        except Exception:
            await self.database.fail_token_usage(reservation["id"])
            raise
        input_tokens, cached, output_tokens, source = self._response_usage(
            response,
            prompt=prompt,
            system=system_prompt_value,
        )
        await self.database.settle_token_usage(
            reservation["id"],
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            output_tokens=output_tokens,
            usage_source=source,
        )
        return response

    @staticmethod
    def _visible_length(value: str) -> int:
        return len(re.sub(r"\s+", "", str(value or "")))

    @classmethod
    def _validate_mobile_resolution(
        cls,
        resolution: Resolution,
        *,
        expected_actor: Mapping[str, Any] | None,
        roster: Sequence[Mapping[str, Any]],
    ) -> Resolution:
        if resolution.mode == "resolve":
            length = cls._visible_length(resolution.narrative)
            if length < 100 or length > 300:
                raise ValueError(
                    f"故事正文必须为 100—300 字，当前为 {length} 字"
                )
        if not resolution.next_choices:
            return resolution
        normalized = cls._validate_choices_for_actor(
            resolution.next_choices,
            expected_actor=expected_actor,
            roster=roster,
        )
        return replace(resolution, next_choices=tuple(normalized))

    @classmethod
    def _validate_choices_for_actor(
        cls,
        choices: Sequence[Mapping[str, Any]],
        *,
        expected_actor: Mapping[str, Any] | None,
        roster: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        expected_id = str(
            (expected_actor or {}).get("id")
            or (expected_actor or {}).get("participant_id")
            or ""
        )
        expected_name = str(
            (expected_actor or {}).get("character_name")
            or (expected_actor or {}).get("display_name")
            or ""
        )
        if not expected_id:
            return [dict(item) for item in choices]
        other_names = {
            str(
                item.get("character_name")
                or item.get("display_name")
                or ""
            ).strip()
            for item in roster
            if isinstance(item, Mapping)
            and str(item.get("id") or "") != expected_id
        }
        other_names.discard("")
        control_words = ("让", "命令", "迫使", "替", "控制", "要求")
        normalized: list[dict[str, Any]] = []
        for item in choices:
            option = dict(item)
            actor_id = str(option.get("actor_id") or "").strip()
            if actor_id and actor_id != expected_id:
                raise ValueError(
                    "行动选项 actor_id 与下一位行动角色不一致"
                )
            option["actor_id"] = expected_id
            text = str(option.get("text") or "")
            if cls._visible_length(text) > 50:
                raise ValueError("行动选项不得超过 50 字")
            for name in other_names:
                if any(
                    marker + name in text
                    for marker in control_words
                ) or text.startswith(name + "决定"):
                    raise ValueError(
                        f"选项越权操控了其他玩家角色 {name}"
                    )
            if expected_name and text.startswith(expected_name + "让"):
                # “自己让别人执行”仍然是替他人决定行动。
                raise ValueError("选项不能借当前角色回合操控他人")
            normalized.append(option)
        return normalized

    @staticmethod
    def _next_actor(
        turn: Mapping[str, Any],
        roster: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        raw_order = turn.get("order")
        order = [
            str(item.get("user_id") or "")
            for item in raw_order
            if isinstance(item, Mapping) and item.get("user_id")
        ] if isinstance(raw_order, list) else []
        current = str(turn.get("current_user_id") or "")
        if not order:
            return {}
        if current in order and len(order) > 1:
            next_user = order[(order.index(current) + 1) % len(order)]
        else:
            next_user = current or order[0]
        item = next(
            (
                dict(entry)
                for entry in roster
                if str(entry.get("group_user_id") or "") == next_user
            ),
            {},
        )
        if not item:
            return {"group_user_id": next_user}
        return {
            "participant_id": item.get("id"),
            "id": item.get("id"),
            "group_user_id": item.get("group_user_id"),
            "character_name": item.get("character_name"),
            "character_code": item.get("character_code"),
            "display_name": item.get("display_name"),
            "profile": item.get("card_profile", {}),
            "stats": item.get("card_stats", {}),
            "runtime_state": item.get("runtime_state", {}),
        }

    @staticmethod
    def _format_story_paragraphs(value: str) -> str:
        paragraphs = [
            part.strip()
            for part in re.split(r"(?:\r?\n){1,}", str(value or ""))
            if part.strip() and part.strip("-") != ""
        ]
        return "\n\n-----------\n\n".join(paragraphs)

    @staticmethod
    def _provider_order(
        primary: str,
        fallbacks: tuple[str, ...],
    ) -> list[str]:
        result: list[str] = []
        for provider_id in (primary, *fallbacks):
            normalized = str(provider_id or "").strip()
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    @staticmethod
    def _check_request_from_payload(
        payload: Mapping[str, Any],
    ) -> CheckRequest:
        return CheckRequest(
            stat=str(payload.get("stat") or "通用"),
            reason=str(payload.get("reason") or "行动存在不确定性"),
            difficulty=int(payload.get("difficulty") or 12),
            modifier=int(payload.get("modifier") or 0),
            risk=str(payload.get("risk") or "controlled"),
            check_type=str(payload.get("check_type") or "standard"),
            advantage_sources=tuple(
                str(item)
                for item in payload.get("advantage_sources", [])
            ),
            disadvantage_sources=tuple(
                str(item)
                for item in payload.get("disadvantage_sources", [])
            ),
            known_consequences=str(
                payload.get("known_consequences") or ""
            ),
            visibility=str(payload.get("visibility") or "public"),
            inspiration_mode=str(
                payload.get("inspiration_mode") or ""
            ),
            participant_ids=tuple(
                str(item)
                for item in payload.get("participant_ids", [])
            ),
            opponent_modifier=int(
                payload.get("opponent_modifier") or 0
            ),
        )

    @classmethod
    def _check_request_from_locked_choice(
        cls,
        workflow: Mapping[str, Any],
    ) -> CheckRequest:
        selected_choice = (
            dict(workflow.get("selected_choice") or {})
            if isinstance(workflow.get("selected_choice"), Mapping)
            else {}
        )
        return cls._check_request_from_payload(
            {
                "stat": selected_choice.get("check_stat") or "通用",
                "reason": (
                    selected_choice.get("text")
                    or "该选项已被标记为必须检定"
                ),
                "difficulty": selected_choice.get("difficulty") or 12,
                "modifier": 0,
                "risk": selected_choice.get("risk") or "controlled",
                "check_type": (
                    selected_choice.get("check_type") or "standard"
                ),
                "advantage_sources": (
                    selected_choice.get("advantage_sources") or []
                ),
                "disadvantage_sources": (
                    selected_choice.get("disadvantage_sources") or []
                ),
                "known_consequences": (
                    selected_choice.get("known_consequences") or ""
                ),
                "inspiration_mode": (
                    workflow.get("inspiration_mode") or ""
                ),
            }
        )

    @staticmethod
    def _dice_result_from_payload(
        payload: Mapping[str, Any],
    ) -> DiceResult:
        return DiceResult(
            die=int(payload.get("die") or 0),
            modifier=int(payload.get("modifier") or 0),
            total=int(payload.get("total") or 0),
            difficulty=int(payload.get("difficulty") or 0),
            outcome=str(payload.get("outcome") or "failure"),
            critical=(
                str(payload["critical"])
                if payload.get("critical")
                else None
            ),
            rolls=tuple(int(item) for item in payload.get("rolls", [])),
            kept=int(payload.get("kept") or 0),
            dice_mode=str(payload.get("dice_mode") or "standard"),
            margin=int(payload.get("margin") or 0),
            risk=str(payload.get("risk") or "controlled"),
            check_type=str(payload.get("check_type") or "standard"),
            advantage_sources=tuple(
                str(item)
                for item in payload.get("advantage_sources", [])
            ),
            disadvantage_sources=tuple(
                str(item)
                for item in payload.get("disadvantage_sources", [])
            ),
            advantages_cancelled=bool(
                payload.get("advantages_cancelled", False)
            ),
            original_rolls=tuple(
                int(item) for item in payload.get("original_rolls", [])
            ),
            rerolled=bool(payload.get("rerolled", False)),
            visibility=str(payload.get("visibility") or "public"),
            members=tuple(
                dict(item)
                for item in payload.get("members", [])
                if isinstance(item, Mapping)
            ),
        )

    @staticmethod
    def _format_dice_result(
        dice: DiceResult,
        stat: str,
    ) -> str:
        labels = {
            "critical_success": "大成功",
            "success": "成功",
            "success_with_cost": "代价成功",
            "failure": "失败",
            "critical_failure": "大失败",
        }
        mode_labels = {
            "standard": "常规",
            "advantage": "优势",
            "disadvantage": "劣势",
            "group": "集体",
        }
        result_label = labels.get(dice.outcome, dice.outcome)
        if dice.visibility == "hidden":
            return ""
        if dice.members and dice.check_type in {"group", "resistance"}:
            member_lines = [
                (
                    f"- {item.get('name') or item.get('actor_id')}: "
                    f"{item.get('rolls')} "
                    f"{int(item.get('modifier') or 0):+d} → "
                    f"{item.get('total')} · "
                    f"{labels.get(str(item.get('outcome')), item.get('outcome'))}"
                )
                for item in dice.members
            ]
            header = (
                f"🎲【{stat}·{mode_labels.get(dice.dice_mode, dice.dice_mode)}"
                f"检定】{dice.total}/{dice.difficulty} 人达标 · "
                f"{result_label}"
            )
            return "\n".join([header, *member_lines])
        rolls = list(dice.rolls)
        pool = (
            f"{rolls} → 取 {dice.kept}"
            if len(rolls) > 1
            else str(dice.kept)
        )
        modifier = f"{dice.modifier:+d}"
        header = (
            f"🎲【{stat}检定】"
            f"［{mode_labels.get(dice.dice_mode, dice.dice_mode)}］"
            f"{pool} {modifier} → {dice.total}"
        )
        if dice.visibility == "public":
            header += f" / DC {dice.difficulty}"
        header += f" · {result_label}"
        if dice.visibility == "public":
            source_lines = []
            if dice.advantage_sources:
                source_lines.append(
                    "优势：" + "；".join(dice.advantage_sources)
                )
            if dice.disadvantage_sources:
                source_lines.append(
                    "劣势：" + "；".join(dice.disadvantage_sources)
                )
            if dice.advantages_cancelled:
                source_lines.append("优劣势同时存在，本次互相抵消")
            if dice.rerolled:
                source_lines.append(
                    f"灵感重投：原骰池 {list(dice.original_rolls)}"
                )
            if dice.check_type == "opposed" and dice.members:
                defender = dice.members[0]
                source_lines.append(
                    "对抗方："
                    f"{defender.get('name') or '防守方'} "
                    f"{defender.get('rolls')} "
                    f"{int(defender.get('modifier') or 0):+d} → "
                    f"{defender.get('total')}"
                )
            if source_lines:
                header += "\n" + "\n".join(source_lines)
        return header

    async def _publish_locked_check_progress(
        self,
        callback: Callable[[str], Any] | None,
        dice: DiceResult,
        stat: str,
    ) -> None:
        dice_text = self._format_dice_result(dice, stat)
        if dice_text:
            await self._emit_progress(callback, dice_text)
        await self._emit_progress(
            callback,
            "【酒馆】已收到你的选择，后续内容正在生成中……",
        )

    async def _story_providers(
        self,
        event: Any,
        config: TavernConfig,
    ) -> list[str]:
        primary = config.provider_id
        current_error: Exception | None = None
        if not primary:
            try:
                primary = await self.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
            except Exception as exc:
                current_error = exc
        providers = self._provider_order(
            primary,
            config.fallback_provider_ids,
        )
        if providers:
            return await self.database.filter_healthy_providers(providers)
        if current_error:
            raise TavernEngineError(
                "无法取得当前群会话模型，且没有配置备用模型"
            ) from current_error
        raise TavernEngineError("没有可用的叙事模型")

    @staticmethod
    def _component_children(component: Any) -> list[Any]:
        for attribute in ("chain", "message", "message_chain"):
            value = getattr(component, attribute, None)
            if isinstance(value, (list, tuple)):
                return list(value)
            nested = getattr(value, "chain", None)
            if isinstance(nested, (list, tuple)):
                return list(nested)
        return []

    async def _image_references(
        self,
        event: Any,
        limit: int,
    ) -> list[str]:
        message_obj = getattr(event, "message_obj", None)
        message = getattr(message_obj, "message", None)
        if isinstance(message, (list, tuple)):
            pending = list(message)
        elif message is None:
            pending = []
        else:
            pending = self._component_children(message)
            if not pending:
                try:
                    pending = list(message)
                except TypeError:
                    pending = [message]
        result: list[str] = []
        seen_components: set[int] = set()
        while pending and len(result) < limit:
            component = pending.pop(0)
            identity = id(component)
            if identity in seen_components:
                continue
            seen_components.add(identity)
            pending.extend(self._component_children(component))
            if component.__class__.__name__.casefold() != "image":
                continue
            reference = str(
                getattr(component, "url", "")
                or getattr(component, "file", "")
                or ""
            ).strip()
            if not reference:
                converter = getattr(component, "convert_to_base64", None)
                if callable(converter):
                    try:
                        converted = converter()
                        if inspect.isawaitable(converted):
                            converted = await converted
                    except Exception as exc:
                        raise TavernEngineError(
                            "无法读取消息中的图片，本条内容未记录"
                        ) from exc
                    encoded = str(converted or "").strip()
                    if encoded:
                        if encoded.startswith(("data:", "base64://")):
                            reference = encoded
                        else:
                            reference = (
                                "data:image/jpeg;base64," + encoded
                            )
            if not reference:
                raise TavernEngineError(
                    "无法读取消息中的图片，本条内容未记录"
                )
            if reference and reference not in result:
                result.append(reference)
        return result

    async def _caption_images(
        self,
        *,
        event: Any,
        session_id: str,
        config: TavernConfig,
    ) -> str:
        image_urls = await self._image_references(
            event,
            config.max_images_per_turn,
        )
        if not image_urls:
            return ""
        if not config.image_caption_provider_id:
            raise TavernEngineError(
                "检测到图片，但尚未配置图片转述模型；"
                "本条内容未记录。"
            )
        try:
            response = await asyncio.wait_for(
                self._llm_generate_metered(
                    session_id=session_id,
                    request_type="image_caption",
                    provider_id=config.image_caption_provider_id,
                    prompt=(
                        f"{config.image_caption_prompt}\n\n"
                        f"共 {len(image_urls)} 张图片，请按“图1、图2……”"
                        "分别描述。"
                    ),
                    image_urls=image_urls,
                    temperature=0.1,
                    max_tokens=min(1200, config.max_tokens),
                ),
                timeout=config.request_timeout_seconds,
            )
        except TimeoutError as exc:
            raise TavernEngineError(
                "图片转述模型请求超时，本条内容未记录"
            ) from exc
        except Exception as exc:
            raise TavernEngineError(
                f"图片转述模型调用失败：{type(exc).__name__}"
            ) from exc
        caption = clean_text(
            getattr(response, "completion_text", ""),
            max_chars=min(6000, config.max_output_chars),
        )
        if not caption:
            raise TavernEngineError("图片转述模型没有返回有效描述")
        return caption

    async def join_player(
        self,
        *,
        session_id: str,
        sender_id: str,
        sender_name: str,
    ) -> dict[str, Any]:
        lock = await self._session_lock(session_id)
        async with lock:
            session = await self.database.get_session(session_id)
            if session["state"] in {"closed", "maintenance"}:
                raise TavernEngineError("酒馆当前不接受玩家加入")
            return await self.database.join_turn_order(
                session_id,
                sender_id,
                sender_name,
                sender_id,
            )

    async def leave_player(
        self,
        *,
        session_id: str,
        sender_id: str,
    ) -> dict[str, Any]:
        lock = await self._session_lock(session_id)
        async with lock:
            return await self.database.leave_turn_order(
                session_id,
                sender_id,
                sender_id,
            )

    async def skip_player(
        self,
        *,
        session_id: str,
        sender_id: str,
        force: bool = False,
        controlled_user_id: str = "",
    ) -> dict[str, Any]:
        lock = await self._session_lock(session_id)
        async with lock:
            session = await self.database.get_session(session_id)
            if session["state"] != "running":
                raise TavernEngineError("酒馆当前不在运行状态")
            target_user_id = controlled_user_id or sender_id
            if target_user_id != sender_id and not force:
                participant = await self.database.get_participant(
                    session_id,
                    user_id=target_user_id,
                )
                control = (
                    await self.database.authorize_participant_control(
                        session_id,
                        participant["id"],
                        sender_id,
                        "skip",
                    )
                )
                if not control["authorized"]:
                    raise TavernEngineError("你没有该角色的有效代控授权")
            return await self.database.skip_turn(
                session_id,
                target_user_id,
                sender_id,
                force=force,
            )

    async def process_choice(
        self,
        *,
        event: Any,
        session_id: str,
        sender_id: str,
        sender_name: str,
        choice_key: str,
        flavor_text: str = "",
        inspiration_mode: str = "",
        progress: Callable[[str], Any] | None = None,
        operator_id: str = "",
        force: bool = False,
    ) -> EngineReply:
        choice_set = await self.database.active_choice_set(session_id)
        if not choice_set:
            vote = await self.database.active_vote(session_id)
            if vote:
                raise TavernEngineError(
                    "当前处于集体投票阶段，请使用 /酒馆 投票 A"
                )
            raise TavernEngineError("当前没有可选择的行动选项")
        participant = choice_set.get("participant")
        if not participant:
            raise TavernEngineError("当前选项没有有效的行动角色")
        control = await self.database.authorize_participant_control(
            session_id,
            participant["id"],
            sender_id,
            "choose",
        )
        if not control["authorized"] and not force:
            owner = (
                participant.get("character_name")
                or participant.get("display_name")
                or participant.get("group_user_id")
            )
            raise TavernTurnOrderError(
                f"当前选项属于 {owner}，本条内容未记录。",
                turn=await self.database.get_turn_status(session_id),
            )
        if force:
            control = {
                "authorized": True,
                "mode": "admin_forced",
                "controller_user_id": sender_id,
                "source": "admin",
                "forced": True,
            }
        key = str(choice_key or "").strip().upper()
        selected = next(
            (
                item
                for item in choice_set["choices"]
                if str(item.get("key") or "").upper() == key
            ),
            None,
        )
        if not selected:
            raise TavernEngineError("请选择 A、B、C 或 D")
        inspiration_mode = str(inspiration_mode or "").strip().lower()
        if inspiration_mode not in {"", "advantage", "reroll"}:
            raise TavernEngineError("灵感用法必须为优势或重投")
        if inspiration_mode and not selected.get("requires_check"):
            raise TavernEngineError("该选项不需要检定，不能消耗灵感点")
        flavor = clean_text(flavor_text, max_chars=160)
        acting_user_id = str(participant["group_user_id"])
        if bool(selected.get("collective")):
            # 0.11.2：全队行动选项 → 由引擎直接发起集体表决，
            # 不再依赖叙事模型自行生成 group_decision（模型未生成时
            # 旧逻辑会把整轮判为“未提交”，玩家陷入死胡同）。
            return await self._start_team_vote(
                session_id=session_id,
                participant=participant,
                selected=selected,
                sender_id=sender_id,
                flavor=flavor,
            )
        content = f"选择 {key}：{selected['text']}"
        if flavor:
            content += f"\n演绎偏好：{flavor}"
        return await self.process(
            event=event,
            session_id=session_id,
            sender_id=acting_user_id,
            sender_name=(
                participant.get("character_name")
                or participant.get("display_name")
                or sender_name
            ),
            content=content,
            workflow={
                "choice_set_id": choice_set["id"],
                "selected_key": key,
                "flavor_text": flavor,
                "requires_check": bool(selected.get("requires_check")),
                "collective": bool(selected.get("collective")),
                "selected_choice": dict(selected),
                "inspiration_mode": inspiration_mode,
                "controller_user_id": sender_id,
                "control_mode": control["mode"],
                "control_source": control.get("source", ""),
            },
            progress=progress,
            operator_id=operator_id,
            force_actor=force,
        )

    async def process_vote_resolution(
        self,
        *,
        event: Any,
        session_id: str,
        vote: Mapping[str, Any],
        progress: Callable[[str], Any] | None = None,
    ) -> EngineReply:
        """0.11.2：集体表决通过后，把表决结果作为已定事实推进剧情并生成新选项。

        修复：旧实现中 `/酒馆 投票` 的“表决通过”分支只发送确认文本，
        从不生成后续叙事与新选项（WebUI 只读展示所以“看起来正常”）。
        """
        config = self.config_provider()
        lock = await self._session_lock(session_id)
        async with lock:
            session = await self.database.get_session(session_id)
            if session["state"] != "running":
                raise TavernEngineError("酒馆当前不在运行状态")
            try:
                instance = await self.database.get_instance_config(
                    session_id
                )
                world = dict(instance["world_snapshot"])
            except Exception:
                world = await self.database.get_world(session["world_id"])
            events = await self.database.recent_events(
                session_id,
                config.recent_turns * 2 + 6,
            )
            roster = await self.database.list_roster(session_id)
            turn = await self.database.get_turn_status(session_id)
            session = dict(session)
            session["roster"] = roster
            session["turn_status"] = turn
            session["next_actor"] = self._next_actor(turn, roster)
            memories = await self.database.list_memories(
                session_id,
                "",
                config.recent_turns * 2 + 6,
            )
            winner_key = str(vote.get("winner_key") or "")
            winning_text = ""
            for option in (vote.get("options") or []):
                if (
                    isinstance(option, Mapping)
                    and str(option.get("key")) == winner_key
                ):
                    winning_text = str(option.get("text") or "")
                    break
            if not winning_text:
                winning_text = winner_key
            vote_input = f"队伍已表决通过：{winning_text}"
            # 0.11.4：全队行动若在投票时声明了检定（如 魔力 DC17），
            # 表决通过后先执行该检定，再把结果作为权威输入生成落实叙事。
            check_definition: dict[str, Any] | None = None
            for option in (vote.get("options") or []):
                if (
                    isinstance(option, Mapping)
                    and isinstance(option.get("check"), Mapping)
                ):
                    check_definition = dict(option["check"])
                    break
            vote_check: CheckRequest | None = None
            vote_dice: DiceResult | None = None
            if check_definition:
                vote_check = self._check_request_from_payload(
                    check_definition
                )
                acting_user_id = str(
                    vote.get("suspended_user_id") or ""
                )
                check_type = str(
                    vote_check.check_type or "standard"
                ).lower()
                if check_type in {"group", "resistance"}:
                    actors: list[dict[str, Any]] = []
                    for member in roster:
                        if (
                            member.get("participation_status") != "active"
                            or member.get("card_status") != "approved"
                        ):
                            continue
                        member_user_id = str(
                            member.get("group_user_id") or ""
                        )
                        member_modifier = (
                            await self.database.authoritative_modifier(
                                session_id,
                                member_user_id,
                                vote_check.stat,
                            )
                        )
                        member_context = (
                            await self.database.check_context(
                                session_id,
                                member_user_id,
                                str(member_modifier["stat"]),
                                proposed_advantages=(
                                    vote_check.advantage_sources
                                ),
                                proposed_disadvantages=(
                                    vote_check.disadvantage_sources
                                ),
                            )
                        )
                        actors.append(
                            {
                                "actor_id": member["id"],
                                "name": (
                                    member.get("character_name")
                                    or member.get("display_name")
                                    or member_user_id
                                ),
                                "modifier": member_modifier["modifier"],
                                "advantage_sources": member_context[
                                    "advantages"
                                ],
                                "disadvantage_sources": member_context[
                                    "disadvantages"
                                ],
                            }
                        )
                    if not actors:
                        raise TavernEngineError(
                            "集体检定没有有效参与角色"
                        )
                    vote_dice = await self._roll_with_registered_system(
                        world, vote_check, actors=actors
                    )
                else:
                    if not acting_user_id:
                        raise TavernEngineError("表决检定缺少执行玩家")
                    authoritative = (
                        await self.database.authoritative_modifier(
                            session_id,
                            acting_user_id,
                            vote_check.stat,
                        )
                    )
                    if (
                        not authoritative.get("matched")
                        and world_contract(world)["resolution"]["mode"]
                        == "attribute"
                    ):
                        raise TavernEngineError(
                            f"检定属性“{vote_check.stat}”不属于当前"
                            "世界或角色卡，表决检定无法执行"
                        )
                    await self.database.check_context(
                        session_id,
                        acting_user_id,
                        str(authoritative["stat"]),
                        proposed_advantages=(
                            vote_check.advantage_sources
                        ),
                        proposed_disadvantages=(
                            vote_check.disadvantage_sources
                        ),
                    )
                    vote_dice = await self._roll_with_registered_system(
                        world, vote_check
                    )
                # 检定凭证落库（幂等，可回放）
                dice_op_id = operation_key(
                    session_id,
                    "dice",
                    turn_no=int(session.get("turn_no") or 0) + 1,
                    actor_id=acting_user_id,
                    source_id=str(vote.get("id") or ""),
                    payload={
                        "selected_key": "team",
                        "stat": str(vote_check.stat or "").casefold(),
                        "check_type": str(
                            vote_check.check_type or ""
                        ).casefold(),
                    },
                )
                locked_receipt = await self.database.lock_check_result(
                    dice_op_id,
                    session_id,
                    asdict(vote_check),
                    asdict(vote_dice),
                )
                # v0.12.0（缺陷修复）：与单人检定路径保持一致——
                # 复用凭证中已锁定的检定与骰面。此前表决路径丢弃返回的
                # 凭证，若模型调用失败后重试，会以「新骰面」生成叙事而
                # 凭证仍保留「旧骰面」，导致回放与展示不一致。
                vote_check = self._check_request_from_payload(
                    locked_receipt["request"]
                )
                vote_dice = self._dice_result_from_payload(
                    locked_receipt["result"]
                )
            providers = await self._story_providers(event, config)
            system = system_prompt(
                world,
                allow_check=False,
                capability_projection=[],
            )
            if vote_check is not None and vote_dice is not None:
                prompt = checked_resolution_prompt(
                    world=world,
                    session=session,
                    player={},
                    player_input=vote_input,
                    events=events,
                    memories=memories,
                    check=asdict(vote_check),
                    dice=asdict(vote_dice),
                )
            else:
                prompt = planning_prompt(
                    world=world,
                    session=session,
                    player={},
                    player_input=vote_input,
                    events=events,
                    memories=memories,
                    allow_checks=False,
                    workflow={},
                )
            resolution, used_provider_id = await self._generate_resolution(
                session_id=session_id,
                request_type="vote_resolution",
                world=world,
                provider_ids=providers,
                system=system,
                prompt=prompt,
                config=config,
            )
            if resolution.mode != "resolve":
                raise TavernEngineError("模型未完成表决后的最终裁定")
            if resolution.check is not None:
                raise TavernEngineError(
                    "表决结果落实不应产生新的检定"
                )
            new_state = apply_state_patch(
                session.get("world_state"),
                self._normalize_state_patch_relationships(
                    resolution.state_patch, roster
                ),
            )
            await self._apply_economy_ops(
                session_id=session_id,
                ops=resolution.raw.get("economy_ops"),
                operation_prefix=f"vote:{session.get('revision')}",
                actor_id=str((vote or {}).get("actor_id") or ""),
            )
            next_participant = next(
                (
                    item
                    for item in roster
                    if str(item.get("id") or "")
                    == str(session["next_actor"].get("id") or "")
                ),
                session["next_actor"],
            )
            resolution = await self._ensure_next_choices(
                resolution=resolution,
                provider_ids=self._provider_order(
                    used_provider_id,
                    tuple(providers),
                ),
                world=world,
                session=session,
                participant=next_participant,
                roster=roster,
                events=events,
                candidate_state=new_state,
                config=config,
            )
            narrative = resolution.narrative.strip()
            if len(narrative) > config.max_output_chars:
                narrative = (
                    narrative[: config.max_output_chars].rstrip() + "…"
                )
            updated = await self.database.commit_vote_resolution(
                session_id=session_id,
                expected_revision=session["revision"],
                narrative=narrative,
                world_state=new_state,
                memories=resolution.memories,
                model_payload={**dict(resolution.raw)},
                workflow={
                    "vote_id": str(vote.get("id") or ""),
                    "next_choices": [
                        dict(item) for item in resolution.next_choices
                    ],
                },
                vote_id=str(vote.get("id") or ""),
            )
            story_body = self._format_story_paragraphs(narrative)
            story_output = f"🌐 【集体决定】\n\n{story_body}"
            if vote_dice is not None and vote_check is not None:
                dice_line = self._format_dice_result(
                    vote_dice, vote_check.stat
                )
                if dice_line:
                    story_output = f"{dice_line}\n\n{story_output}"
            next_turn = await self.database.get_turn_status(session_id)
            next_name = (
                str(next_turn.get("current_name") or "")
                or str(next_turn.get("current_user_id") or "")
                or "等待玩家加入"
            )
            turn_output = (
                f"⚔️ 【回合秩序】第 {next_turn.get('round_no', 1)} 轮 · "
                f"当前：{next_name}"
            )
            if resolution.next_choices:
                turn_output += "\n\n" + format_choices(
                    next_participant.get("character_name")
                    or next_participant.get("display_name")
                    or next_name,
                    resolution.next_choices,
                    rerolls_left=1,
                )
            return EngineReply(
                text=f"{story_output}\n\n{turn_output}",
                session=updated,
                dice=vote_dice,
                turn=next_turn,
                story_text=story_output,
                turn_text=turn_output,
            )

    async def process_team_proposal(
        self,
        *,
        event: Any,
        session_id: str,
        sender_id: str,
        sender_name: str,
        index: int = 0,
    ) -> EngineReply:
        """0.11.3：通过 jg 全队 / /酒馆 全队 便捷指令发起全队行动表决。

        全队行动不再占用个人选项的 A—D 字母，玩家用独立指令选择；
        本方法与 process_choice 的 collective 分支共用同一发起逻辑。
        """
        choice_set = await self.database.active_choice_set(session_id)
        if not choice_set:
            raise TavernEngineError("当前没有可选择的行动选项")
        participant = choice_set.get("participant")
        if not participant:
            raise TavernEngineError("当前选项没有有效的行动角色")
        team_choices = [
            item
            for item in choice_set["choices"]
            if bool(item.get("collective"))
        ]
        if not team_choices:
            raise TavernEngineError("当前没有全队行动候选项")
        if index < 0 or index >= len(team_choices):
            raise TavernEngineError(
                f"全队行动编号无效，当前有 {len(team_choices)} 项"
            )
        control = await self.database.authorize_participant_control(
            session_id,
            participant["id"],
            sender_id,
            "choose",
        )
        if not control["authorized"]:
            owner = (
                participant.get("character_name")
                or participant.get("display_name")
                or participant.get("group_user_id")
            )
            raise TavernTurnOrderError(
                f"当前行动属于 {owner}，本条内容未记录。",
                turn=await self.database.get_turn_status(session_id),
            )
        selected = team_choices[index]
        return await self._start_team_vote(
            session_id=session_id,
            participant=participant,
            selected=selected,
            sender_id=sender_id,
        )

    async def _start_team_vote(
        self,
        *,
        session_id: str,
        participant: Mapping[str, Any],
        selected: Mapping[str, Any],
        sender_id: str,
        flavor: str = "",
    ) -> EngineReply:
        """发起「全队行动」的集体表决，不经过模型、不消耗行动机会。"""
        # 0.11.4：若该全队行动声明需要检定（如 魔力 DC17），把检定定义
        # 随「同意执行」选项写入投票，表决通过后据此执行检定。
        team_options: list[dict[str, Any]] = [
            {"key": "A", "text": "同意执行（推进）"}
        ]
        if selected.get("requires_check") and isinstance(
            selected.get("check"), Mapping
        ):
            chk = selected["check"]
            team_options[0]["check"] = {
                "stat": str(
                    chk.get("attribute_label")
                    or chk.get("attribute_id")
                    or chk.get("stat")
                    or "通用"
                ),
                "reason": str(
                    chk.get("reason") or "全队行动存在不确定性"
                ),
                "difficulty": chk.get("difficulty") or 12,
                "risk": str(selected.get("risk") or "controlled"),
                "check_type": str(chk.get("type") or "standard"),
                "advantage_sources": list(
                    chk.get("advantage_sources") or []
                ),
                "disadvantage_sources": list(
                    chk.get("disadvantage_sources") or []
                ),
                "known_consequences": str(
                    chk.get("known_consequences") or ""
                ),
            }
        team_options.append({"key": "B", "text": "暂缓，先处理当前局面"})
        await self.database.create_group_vote(
            session_id,
            group_decision={
                "question": (
                    f"是否执行全队行动：{selected['text']}"
                    + (f"\n补充说明：{flavor}" if flavor else "")
                ),
                "options": team_options,
            },
            suspended_user_id=str(participant["group_user_id"]),
            actor_id=sender_id,
        )
        check_note = ""
        if team_options[0].get("check"):
            chk = team_options[0]["check"]
            stat = str(chk.get("stat") or "通用")
            dc = chk.get("difficulty")
            check_note = (
                f"\n⚠️ 表决通过后将执行检定：{stat}检定"
                + (f" DC{dc}" if dc else "")
            )
        return EngineReply(
            text=(
                "🌐 【集体表决】已发起全员投票，等待全体成员表决。\n"
                f"表决事项：{selected['text']}{check_note}\n\n"
                "💬 请全体成员发送：/酒馆 投票 A（同意执行）"
                "或 B（暂缓）。\n"
                "投票不消耗个人行动机会。"
            ),
            session=await self.database.get_session(session_id),
            turn=await self.database.get_turn_status(session_id),
        )

    async def process_dm_beat(
        self,
        *,
        event: Any,
        session_id: str,
        dm_user_id: str,
        instruction: str,
        progress: Callable[[str], Any] | None = None,
    ) -> dict[str, Any]:
        """Generate and atomically commit a DM beat without consuming a player turn."""
        config = self.config_provider()
        text = clean_text(instruction, max_chars=config.max_input_chars)
        if not text:
            raise TavernEngineError("主持推进方向不能为空")
        lock = await self._session_lock(session_id)
        async with lock:
            session = await self.database.get_session(session_id)
            control = await self.database.get_control_state(session_id)
            if session["state"] != "running":
                raise TavernEngineError("暂停或非运行状态不能主持推进")
            if control["mode"] != "dm":
                raise TavernEngineError("当前未开启主持模式")
            if str(control["active_dm_user_id"]) != str(dm_user_id):
                raise TavernEngineError("只有当前活动 DM 可以推进剧情")
            try:
                instance = await self.database.get_instance_config(session_id)
                world = dict(instance["world_snapshot"])
            except Exception:
                world = await self.database.get_world(session["world_id"])
            roster = await self.database.list_roster(session_id)
            turn = await self.database.get_turn_status(session_id)
            session = dict(session)
            session["roster"] = roster
            session["turn_status"] = turn
            session["next_actor"] = {}
            session["return_requests"] = await self.database.list_return_requests(session_id)
            session["session_characters"] = await self.database.list_session_characters(
                session_id, include_archived=False, context_only=True
            )
            session["story_ledger"] = await self.database.list_story_ledger(session_id)
            session["scene_clocks"] = await self.database.list_scene_clocks(session_id)
            rule_state = await self.database.get_session_rule_state(session_id)
            session["content_boundaries"] = rule_state.get("content_boundaries", {})
            events = await self.database.recent_events(
                session_id, config.recent_turns * 2 + 6
            )
            memories = await self.database.list_memories(
                session_id, text, config.memory_limit
            )
            providers = await self._story_providers(event, config)
            await self._emit_progress(progress, "【主持推进】已收到导演指令，正在生成……")
            resolution, provider_id = await self._generate_resolution(
                session_id=session_id,
                request_type="dm_beat",
                world=world,
                provider_ids=providers,
                system=system_prompt(world, allow_check=False),
                prompt=dm_beat_prompt(
                    world=world,
                    session=session,
                    instruction=text,
                    directive=str(control.get("directive") or ""),
                    events=events,
                    memories=memories,
                ),
                config=config,
            )
            if resolution.mode != "resolve" or resolution.check is not None:
                raise TavernEngineError("主持推进不得申请检定")
            if resolution.group_decision:
                raise TavernEngineError("主持推进不得直接创建集体投票")
            narrative = clean_text(
                resolution.narrative, max_chars=config.max_output_chars
            )
            new_state = apply_state_patch(
                session.get("world_state"),
                self._normalize_state_patch_relationships(
                    resolution.state_patch, roster
                ),
            )
            await self._apply_economy_ops(
                session_id=session_id,
                ops=resolution.raw.get("economy_ops"),
                operation_prefix=f"dm:{session.get('revision')}",
                actor_id=dm_user_id,
                source="dm",
            )
            workflow = {
                "npc_ops": [dict(item) for item in resolution.npc_ops],
                "clock_ops": [dict(item) for item in resolution.clock_ops],
                "ledger_ops": [dict(item) for item in resolution.ledger_ops],
                "status_ops": [dict(item) for item in resolution.status_ops],
                "assist_ops": [dict(item) for item in resolution.assist_ops],
            }
            result = await self.database.commit_dm_beat(
                session_id=session_id,
                expected_revision=int(session["revision"]),
                dm_user_id=dm_user_id,
                instruction=text,
                narrative=narrative,
                world_state=new_state,
                memories=[dict(item) for item in resolution.memories],
                model_payload={**dict(resolution.raw), "_provider": provider_id},
                workflow=workflow,
            )
            await self.broker.publish(
                {
                    "type": "dm_control",
                    "hook": "dm_beat_committed",
                    "session_id": session_id,
                    "beat_no": result["beat_no"],
                    "actor": dm_user_id,
                }
            )
            return result

    async def reroll_choices(
        self,
        *,
        event: Any,
        session_id: str,
        sender_id: str,
    ) -> dict[str, Any]:
        choice_set = await self.database.active_choice_set(session_id)
        if not choice_set or not choice_set.get("participant"):
            raise TavernEngineError("当前没有可重整的个人行动选项")
        participant = choice_set["participant"]
        control = await self.database.authorize_participant_control(
            session_id,
            participant["id"],
            sender_id,
            "reroll",
        )
        if not control["authorized"]:
            raise TavernEngineError("只能重整自己当前回合的选项")
        if int(choice_set["reroll_count"]) >= 1:
            raise TavernEngineError("本回合的免费重整次数已经用完")
        config = self.config_provider()
        session = await self.database.get_session(session_id)
        if session["state"] != "running":
            raise TavernEngineError("酒馆当前不在运行状态，无法重整选项")
        roster = await self.database.list_roster(session_id)
        rich_participant = next(
            (
                item
                for item in roster
                if item.get("id") == participant.get("id")
            ),
            None,
        )
        if rich_participant:
            participant = rich_participant
        try:
            instance = await self.database.get_instance_config(session_id)
            world = dict(instance["world_snapshot"])
        except Exception:
            world = await self.database.get_world(session["world_id"])
        events = await self.database.recent_events(
            session_id,
            config.recent_turns * 2 + 6,
        )
        providers = await self._story_providers(event, config)
        choices = await self._generate_choices(
            provider_ids=providers,
            world=world,
            session=session,
            participant=participant,
            events=events,
            config=config,
            avoid=choice_set["choices"],
            roster=roster,
        )
        result = await self.database.replace_active_choices(
            session_id,
            participant["id"],
            choices,
            actor_id=sender_id,
        )
        result["participant"] = participant
        return result

    async def process(
        self,
        *,
        event: Any,
        session_id: str,
        sender_id: str,
        sender_name: str,
        content: str,
        workflow: Mapping[str, Any] | None = None,
        progress: Callable[[str], Any] | None = None,
        operator_id: str = "",
        force_actor: bool = False,
    ) -> EngineReply:
        config = self.config_provider()
        text = clean_text(content, max_chars=config.max_input_chars)
        if not text:
            raise TavernEngineError("行动内容为空")

        preflight_session = await self.database.get_session(session_id)
        if preflight_session["state"] != "running":
            raise TavernEngineError("酒馆当前不在运行状态")
        preflight_turn = await self.database.get_turn_status(session_id)
        if (
            not force_actor
            and preflight_turn["current_user_id"]
            and preflight_turn["current_user_id"] != sender_id
        ):
            current = (
                preflight_turn["current_name"]
                or preflight_turn["current_user_id"]
            )
            members = {
                str(item.get("user_id") or "")
                for item in preflight_turn["order"]
            }
            join_note = (
                "你尚未加入队列，请先发送 /酒馆 加入；"
                if sender_id not in members
                else ""
            )
            raise TavernTurnOrderError(
                f"{join_note}当前轮到 {current}，本条内容未记录。",
                turn=preflight_turn,
            )

        lock = await self._session_lock(session_id)
        async with lock:
            session = await self.database.get_session(session_id)
            if session["state"] != "running":
                raise TavernEngineError("酒馆当前不在运行状态")
            if int(session.get("input_locked") or 0) and not force_actor:
                raise TavernEngineError("副本输入已被 DM 锁定，请等待解锁")

            remaining = self.rate_limiter.remaining(
                session_id,
                sender_id,
                config.user_cooldown_seconds,
            )
            if remaining > 0:
                raise TavernBusyError(
                    f"行动提交过快，请等待 {remaining:.1f} 秒"
                )

            try:
                joined_result = await self.database.join_turn_order(
                    session_id,
                    sender_id,
                    sender_name,
                    sender_id,
                )
            except InvalidTransitionError as exc:
                if "玩家身份" in str(exc):
                    raise TavernPlayerDisabledError(str(exc)) from exc
                raise
            player = joined_result["player"]
            if not player["enabled"]:
                raise TavernPlayerDisabledError("该玩家已被停用")
            session = joined_result["session"]
            turn = joined_result["turn"]
            if not force_actor and turn["current_user_id"] != sender_id:
                current = turn["current_name"] or turn["current_user_id"]
                joined_note = "你已加入队尾；" if joined_result["joined"] else ""
                raise TavernTurnOrderError(
                    f"{joined_note}当前轮到 {current}，本条内容未记录。",
                    turn=turn,
                    joined=joined_result["joined"],
                )
            acting_round = int(turn["round_no"])
            if operator_id and operator_id != sender_id:
                try:
                    await self.database.write_audit(
                        session_id,
                        operator_id,
                        "turn.forced_choose",
                        sender_id,
                        {
                            "actor_user_id": sender_id,
                            "force": True,
                            "operator_id": operator_id,
                        },
                    )
                except Exception:
                    logger.exception("AI 酒馆强制代选审计写入失败")

            for prefix in config.ooc_prefixes:
                if text.lower().startswith(prefix.lower()):
                    await self.database.append_ooc(
                        session_id,
                        sender_id,
                        sender_name,
                        text,
                    )
                    await self.broker.publish(
                        {
                            "type": "ooc",
                            "session_id": session_id,
                            "actor": sender_name,
                        }
                    )
                    return EngineReply(
                        text=(
                            "【OOC】场外发言已记录，"
                            "本轮世界状态与行动顺序均未推进。"
                        ),
                        session=session,
                        ooc=True,
                        turn=turn,
                    )

            image_caption = await self._caption_images(
                event=event,
                session_id=session_id,
                config=config,
            )
            player_input = text
            if image_caption:
                player_input = (
                    f"{text}\n\n"
                    "<image_descriptions>\n"
                    f"{image_caption}\n"
                    "</image_descriptions>"
                )

            current_world = await self.database.get_world(session["world_id"])
            try:
                instance = await self.database.get_instance_config(session_id)
                world = dict(instance["world_snapshot"])
                world.setdefault(
                    "characters",
                    current_world.get("characters", []),
                )
            except Exception:
                world = current_world
            players = await self.database.list_players(session_id)
            roster = await self.database.list_roster(session_id)
            acting_participant = next(
                (
                    item
                    for item in roster
                    if str(item.get("group_user_id") or "") == sender_id
                ),
                None,
            )
            if (
                acting_participant
                and int(acting_participant.get("action_locked") or 0)
                and not force_actor
            ):
                raise TavernEngineError(
                    "该角色的行动已被 DM 锁定，请等待解锁"
                )
            player = dict(player)
            if acting_participant:
                player.update(
                    {
                        "participant_id": acting_participant.get("id"),
                        "character_name": (
                            acting_participant.get("character_name")
                            or player.get("character_name")
                        ),
                        "character_code": acting_participant.get(
                            "character_code"
                        ),
                        "profile": acting_participant.get(
                            "card_profile",
                            {},
                        ),
                        "stats": acting_participant.get("card_stats", {}),
                        "runtime_state": acting_participant.get(
                            "runtime_state",
                            {},
                        ),
                        "participation_status": acting_participant.get(
                            "participation_status"
                        ),
                    }
                )
            session = dict(session)
            session["players"] = players
            session["roster"] = roster
            session["turn_status"] = turn
            session["next_actor"] = self._next_actor(turn, roster)
            session["return_requests"] = await self.database.list_return_requests(
                session_id
            )
            rule_state = await self.database.get_session_rule_state(session_id)
            context_budget = dict(rule_state.get("context_budget") or {})
            recent_turn_limit = max(
                2,
                min(
                    50,
                    int(
                        context_budget.get(
                            "recent_turns",
                            config.recent_turns,
                        )
                    ),
                ),
            )
            memory_limit = max(
                0,
                min(
                    40,
                    int(
                        context_budget.get(
                            "memories",
                            config.memory_limit,
                        )
                    ),
                ),
            )
            events = await self.database.recent_events(
                session_id,
                recent_turn_limit * 2 + 6,
            )
            memories = await self.database.list_memories(
                session_id,
                player_input,
                memory_limit,
            )
            session["session_characters"] = (
                await self.database.list_session_characters(
                    session_id,
                    include_archived=False,
                    context_only=True,
                )
            )
            ledger_limit = max(
                0,
                min(100, int(context_budget.get("ledger_items", 8))),
            )
            session["story_ledger"] = (
                await self.database.list_story_ledger(session_id)
            )[:ledger_limit]
            session["scene_clocks"] = await self.database.list_scene_clocks(
                session_id
            )
            session["content_boundaries"] = rule_state.get(
                "content_boundaries",
                {},
            )
            session["progress"] = rule_state.get("progress", {})
            session["recovery"] = rule_state.get("recovery", {})
            provider_ids = await self._story_providers(event, config)

            transport_id = transport_event_id(event)
            operation_turn = (
                0 if transport_id else int(session.get("turn_no", 0)) + 1
            )
            turn_operation_id = operation_key(
                session_id,
                "turn",
                turn_no=operation_turn,
                actor_id=sender_id,
                source_id=str(
                    transport_id
                    or (workflow or {}).get("choice_set_id")
                    or session.get("revision")
                    or ""
                ),
                payload=(
                    {"transport_event_id": transport_id}
                    if transport_id
                    else {
                        "input": player_input,
                        "selected_key": str((workflow or {}).get("selected_key") or ""),
                    }
                ),
            )
            operation = await self.database.reserve_operation(
                turn_operation_id,
                session_id,
                "turn",
                {
                    "turn_no": operation_turn,
                    "transport_event_id": transport_id,
                    "actor_id": sender_id,
                    "choice_set_id": str((workflow or {}).get("choice_set_id") or ""),
                    "selected_key": str((workflow or {}).get("selected_key") or ""),
                },
            )
            if not operation.get("created"):
                if operation.get("status") == "completed":
                    raise TavernBusyError("该行动已经处理完成，重复事件未再次消费")
                if operation.get("status") == "pending":
                    phase = str((operation.get("result") or {}).get("phase") or "生成中")
                    raise TavernBusyError(f"该行动正在处理中（{phase}），请勿重复提交")
                await self.database.update_operation(
                    turn_operation_id,
                    status="pending",
                    phase="retrying",
                    result={"retry": True},
                )

            allow_unlocked_check = bool(
                not workflow
                and config.two_phase_checks
                and world_contract(world)["resolution"]["mode"]
                in {"dice_only", "attribute"}
            )
            capability_projection = []
            if acting_participant:
                capability_projection = await self.database.list_actor_capabilities(
                    session_id,
                    f"character:{acting_participant.get('id')}",
                )
            system = system_prompt(
                world,
                allow_check=allow_unlocked_check,
                capability_projection=capability_projection,
            )
            first_prompt = planning_prompt(
                world=world,
                session=session,
                player=player,
                player_input=player_input,
                events=events,
                memories=memories,
                allow_checks=(config.two_phase_checks and world_contract(world)["resolution"]["mode"] in {"dice_only", "attribute"}),
                workflow=workflow,
            )
            generation_notice_sent = False
            if workflow and workflow.get("requires_check"):
                locked_check = self._check_request_from_locked_choice(workflow)
                resolution = Resolution(
                    mode="check",
                    narrative="",
                    check=locked_check,
                    state_patch={},
                    memories=(),
                    next_choices=(),
                    group_decision=None,
                    return_progress=None,
                    npc_ops=(),
                    clock_ops=(),
                    ledger_ops=(),
                    status_ops=(),
                    assist_ops=(),
                    director_note="",
                    raw={
                        "mode": "check",
                        "source": "plugin_locked_choice",
                    },
                )
                used_provider_id = ""
            else:
                await self._emit_progress(
                    progress,
                    "【酒馆】已收到你的选择，后续内容正在生成中……",
                )
                generation_notice_sent = True
                try:
                    resolution, used_provider_id = await self._generate_resolution(
                        session_id=session_id,
                        request_type="story_plan",
                        world=world,
                        provider_ids=provider_ids,
                        system=system,
                        prompt=first_prompt,
                        config=config,
                        expected_actor=session["next_actor"],
                        roster=roster,
                        enforce_mobile_limits=bool(
                            workflow and config.enforce_mobile_output
                        ),
                    )
                except Exception as exc:
                    await self.database.update_operation(
                        turn_operation_id,
                        status="failed",
                        phase="story_plan_failed",
                        result={
                            "error_type": type(exc).__name__,
                            "error": clean_text(str(exc), max_chars=500),
                        },
                    )
                    raise
            await self.database.update_operation(
                turn_operation_id,
                phase="story_plan_generated",
                status="pending",
            )

            dice: DiceResult | None = None
            check_request = None
            first_mode = resolution.mode
            if (
                workflow
                and workflow.get("requires_check")
                and first_mode != "check"
            ):
                check_request = self._check_request_from_locked_choice(
                    workflow
                )
                resolution = replace(
                    resolution,
                    mode="check",
                    narrative="",
                    check=check_request,
                )
                first_mode = "check"
            if (
                workflow
                and not workflow.get("requires_check")
                and first_mode == "check"
            ):
                raise TavernEngineError(
                    "该选项未提前标记检定与风险，但模型临时申请投骰；"
                    "为避免隐藏加码，本轮没有提交"
                )
            if resolution.mode == "check":
                if resolution.check is None:
                    raise TavernEngineError("模型检定结构缺失")
                check_request = resolution.check
                # 0.11.3：记录骰值锁定键，用于“本轮未提交”时作废已锁骰值。
                operation_id: str | None = None
                selected_choice = (
                    dict(workflow.get("selected_choice") or {})
                    if workflow
                    and isinstance(workflow.get("selected_choice"), Mapping)
                    else {}
                )
                locked_advantages = tuple(
                    selected_choice.get("advantage_sources") or ()
                )
                locked_disadvantages = tuple(
                    selected_choice.get("disadvantage_sources") or ()
                )
                effective_stat = str(
                    selected_choice.get("check_stat")
                    or check_request.stat
                    or "通用"
                )
                authoritative = await self.database.authoritative_modifier(
                    session_id, sender_id, effective_stat,
                )
                if world_contract(world)["resolution"]["mode"] == "attribute" and not authoritative.get("matched"):
                    raise TavernEngineError(f"检定属性“{effective_stat}”不属于当前世界或角色卡，本轮没有投骰")
                check_context = await self.database.check_context(
                    session_id,
                    sender_id,
                    str(authoritative["stat"]),
                    proposed_advantages=check_request.advantage_sources,
                    proposed_disadvantages=check_request.disadvantage_sources,
                    locked_advantages=locked_advantages,
                    locked_disadvantages=locked_disadvantages,
                )
                inspiration_mode = str(
                    workflow.get("inspiration_mode") if workflow else ""
                ).lower()
                if inspiration_mode:
                    inspiration = await self.database.inspiration_status(
                        session_id,
                        sender_id,
                    )
                    if inspiration["balance"] < 1:
                        raise TavernEngineError("灵感点不足，本轮没有投骰")
                check_type = str(
                    selected_choice.get("check_type")
                    or check_request.check_type
                    or "standard"
                )
                if (
                    inspiration_mode
                    and check_type in {"group", "resistance"}
                ):
                    raise TavernEngineError(
                        "集体检定与独立抵抗不能由一名玩家替全队消耗灵感"
                    )
                dice_visibility = str(
                    (rule_state.get("dice_rules") or {}).get(
                        "visibility",
                        "public",
                    )
                ).lower()
                risk = str(
                    selected_choice.get("risk")
                    or check_request.risk
                    or "controlled"
                )
                known_consequences = str(
                    selected_choice.get("known_consequences")
                    or check_request.known_consequences
                    or ""
                )
                if risk == "lethal" and not known_consequences:
                    raise TavernEngineError(
                        "致命风险没有提前明示已知后果，本轮没有投骰"
                    )
                check_request = replace(
                    check_request,
                    stat=str(authoritative["stat"]),
                    modifier=int(authoritative["modifier"]),
                    difficulty=int(
                        selected_choice.get("difficulty")
                        or check_request.difficulty
                    ),
                    risk=risk,
                    check_type=check_type,
                    advantage_sources=tuple(check_context["advantages"]),
                    disadvantage_sources=tuple(
                        check_context["disadvantages"]
                    ),
                    known_consequences=known_consequences,
                    visibility=(
                        dice_visibility
                        if dice_visibility
                        in {"public", "immersive", "hidden"}
                        else "public"
                    ),
                    inspiration_mode=inspiration_mode,
                    opponent_modifier=0,
                )
                if workflow is not None:
                    workflow = {
                        **dict(workflow),
                        "assist_token_id": check_context.get(
                            "assist_token_id",
                            "",
                        ),
                    }
                operation_id = operation_key(
                    session_id,
                    "dice",
                    turn_no=operation_turn,
                    actor_id=sender_id,
                    source_id=str(
                        workflow.get("choice_set_id")
                        if workflow
                        else session["revision"]
                    ),
                    # 0.11.2：骰值锁定键必须含检定类别与所选选项。
                    # 旧实现只含 session_id+choice_set_id，导致同选项集内
                    # “魅力检定”与“信仰检定”命中同一键、复用上一轮骰值。
                    payload={
                        "selected_key": (
                            str(workflow.get("selected_key") or "")
                            if workflow
                            else ""
                        ),
                        "stat": str(check_request.stat or "").casefold(),
                        "check_type": str(
                            check_request.check_type or ""
                        ).casefold(),
                    },
                )
                receipt = await self.database.get_operation_receipt(
                    operation_id
                )
                if receipt:
                    locked_request = self._check_request_from_payload(
                        receipt["request"]
                    )
                    same_category = (
                        str(locked_request.stat or "").casefold()
                        == str(check_request.stat or "").casefold()
                        and str(
                            locked_request.check_type or ""
                        ).casefold()
                        == str(check_request.check_type or "").casefold()
                    )
                    if not same_category:
                        # 0.11.2 双保险：即使键碰撞，类别不同也绝不复用旧骰值。
                        receipt = None
                    else:
                        if (
                            locked_request.inspiration_mode
                            != check_request.inspiration_mode
                        ):
                            raise TavernEngineError(
                                "本次检定的骰池已经锁定，不能在重试时更换灵感用法"
                            )
                        check_request = locked_request
                        dice = self._dice_result_from_payload(receipt["result"])
                else:
                    if check_type in {"group", "resistance"}:
                        requested_ids = set(check_request.participant_ids)
                        actors: list[dict[str, Any]] = []
                        for member in await self.database.list_roster(
                            session_id
                        ):
                            if (
                                member.get("participation_status") != "active"
                                or member.get("card_status") != "approved"
                            ):
                                continue
                            if requested_ids and not (
                                {
                                    str(member.get("id") or ""),
                                    str(member.get("group_user_id") or ""),
                                }
                                & requested_ids
                            ):
                                continue
                            member_user_id = str(
                                member.get("group_user_id") or ""
                            )
                            member_modifier = (
                                await self.database.authoritative_modifier(
                                    session_id,
                                    member_user_id,
                                    check_request.stat,
                                )
                            )
                            member_context = (
                                await self.database.check_context(
                                    session_id,
                                    member_user_id,
                                    str(member_modifier["stat"]),
                                    proposed_advantages=(
                                        check_request.advantage_sources
                                    ),
                                    proposed_disadvantages=(
                                        check_request.disadvantage_sources
                                    ),
                                    locked_advantages=locked_advantages,
                                    locked_disadvantages=locked_disadvantages,
                                )
                            )
                            actors.append(
                                {
                                    "actor_id": member["id"],
                                    "name": (
                                        member.get("character_name")
                                        or member.get("display_name")
                                        or member_user_id
                                    ),
                                    "modifier": member_modifier["modifier"],
                                    "advantage_sources": member_context[
                                        "advantages"
                                    ],
                                    "disadvantage_sources": member_context[
                                        "disadvantages"
                                    ],
                                }
                            )
                        if not actors:
                            raise TavernEngineError(
                                "集体检定没有有效参与角色"
                            )
                        dice = await self._roll_with_registered_system(
                            world, check_request, actors=actors
                        )
                    else:
                        dice = await self._roll_with_registered_system(
                            world, check_request
                        )
                    receipt = await self.database.lock_check_result(
                        operation_id,
                        session_id,
                        asdict(check_request),
                        asdict(dice),
                    )
                    check_request = self._check_request_from_payload(
                        receipt["request"]
                    )
                    dice = self._dice_result_from_payload(
                        receipt["result"]
                    )
                await self.database.update_operation(
                    turn_operation_id,
                    phase="dice_locked",
                    status="pending",
                    result={
                        "dice_operation_id": operation_id,
                        "outcome": dice.outcome,
                    },
                )
                await self.broker.publish(
                    {
                        "type": "check",
                        "hook": "check_completed",
                        "session_id": session_id,
                        "actor": sender_name,
                        "stat": check_request.stat,
                        "outcome": dice.outcome,
                        "total": dice.total,
                        "difficulty": dice.difficulty,
                    }
                )
                if not generation_notice_sent:
                    await self._publish_locked_check_progress(
                        progress, dice, check_request.stat
                    )
                    generation_notice_sent = True
                else:
                    dice_text = self._format_dice_result(
                        dice, check_request.stat
                    )
                    if dice_text:
                        await self._emit_progress(progress, dice_text)
                check_prompt = checked_resolution_prompt(
                    world=world,
                    session=session,
                    player=player,
                    player_input=player_input,
                    events=events,
                    memories=memories,
                    check=asdict(check_request),
                    dice=asdict(dice),
                )
                second_stage_providers = self._provider_order(
                    used_provider_id,
                    tuple(provider_ids),
                )
                resolution, used_provider_id = (
                    await self._generate_resolution(
                        session_id=session_id,
                        request_type="story_checked",
                        world=world,
                        provider_ids=second_stage_providers,
                        system=system_prompt(
                            world,
                            allow_check=False,
                            capability_projection=capability_projection,
                        ),
                        prompt=check_prompt,
                        config=config,
                        expected_actor=session["next_actor"],
                        roster=roster,
                        enforce_mobile_limits=bool(
                            workflow and config.enforce_mobile_output
                        ),
                    )
                )
                if resolution.mode != "resolve":
                    raise TavernEngineError("模型未完成检定后的最终裁定")

            normalized_patch = self._normalize_state_patch_relationships(
                resolution.state_patch, roster
            )
            new_state = (
                dict(session.get("world_state") or {})
                if resolution.group_decision
                else apply_state_patch(
                    session.get("world_state"),
                    normalized_patch,
                )
            )
            await self._apply_economy_ops(
                session_id=session_id,
                ops=resolution.raw.get("economy_ops"),
                operation_prefix=f"story:{session.get('revision')}",
                actor_id=sender_id,
            )
            if workflow:
                if workflow.get("requires_check") and first_mode != "check":
                    raise TavernEngineError(
                        "该选项标记为必须检定，但模型未申请检定；"
                        "为避免越权结果，本轮没有提交"
                    )
                if (
                    workflow.get("collective")
                    and not resolution.group_decision
                ):
                    # 0.11.3：本轮未提交 → 作废已锁骰值，避免重试复用旧骰。
                    if operation_id:
                        try:
                            await self.database.revoke_operation_receipt(
                                operation_id
                            )
                        except Exception:
                            pass
                    raise TavernEngineError(
                        "该选项影响全队，但模型没有生成集体表决；"
                        "为避免单人越权，本轮没有提交"
                    )
                if resolution.group_decision:
                    resolution = replace(
                        resolution,
                        next_choices=(),
                    )
                else:
                    expected_id = str(
                        session["next_actor"].get("id")
                        or session["next_actor"].get("participant_id")
                        or ""
                    )
                    next_participant = next(
                        (
                            item
                            for item in roster
                            if str(item.get("id") or "") == expected_id
                        ),
                        session["next_actor"],
                    )
                    resolution = await self._ensure_next_choices(
                        resolution=resolution,
                        provider_ids=self._provider_order(
                            used_provider_id,
                            tuple(provider_ids),
                        ),
                        world=world,
                        session=session,
                        participant=next_participant,
                        roster=roster,
                        events=events,
                        candidate_state=new_state,
                        config=config,
                    )
                if not resolution.next_choices and not resolution.group_decision:
                    raise TavernEngineError(
                        "模型未生成下一位玩家的 A/B/C/D 选项；"
                        "本轮没有提交"
                    )
            normalized_memories = []
            for memory in resolution.memories:
                entry = dict(memory)
                if entry["scope"] == "player" and not entry["scope_id"]:
                    entry["scope_id"] = player["id"]
                normalized_memories.append(entry)

            narrative = resolution.narrative.strip()
            if len(narrative) > config.max_output_chars:
                narrative = narrative[: config.max_output_chars].rstrip() + "…"

            quality = inspect_narrative(
                narrative,
                [dict(item) for item in resolution.next_choices],
                acting_name=str(sender_id),
                previous_narrative=str(
                    next(
                        (
                            item.get("content")
                            for item in reversed(events)
                            if item.get("role") == "narrator"
                        ),
                        "",
                    )
                    or ""
                ),
            )
            if not quality["passed"]:
                await self.database.update_operation(
                    turn_operation_id,
                    status="failed",
                    phase="quality_rejected",
                    result={"quality": quality},
                )
                raise TavernEngineError("叙事质量检查未通过，本轮没有提交")

            check_payload = asdict(dice) if dice else None
            if dice and check_request:
                check_payload = {
                    **check_payload,
                    "check_id": (
                        "check:"
                        + str(
                            workflow.get("choice_set_id")
                            if workflow
                            else session["revision"]
                        )
                    ),
                    "stat": check_request.stat,
                    "reason": check_request.reason,
                    "known_consequences": (
                        check_request.known_consequences
                    ),
                }
            commit_workflow = (
                {
                    **dict(workflow),
                    "next_choices": [
                        dict(item) for item in resolution.next_choices
                    ],
                    "group_decision": resolution.group_decision,
                    "return_progress": resolution.return_progress,
                    "npc_ops": [
                        dict(item) for item in resolution.npc_ops
                    ],
                    "clock_ops": [
                        dict(item) for item in resolution.clock_ops
                    ],
                    "ledger_ops": [
                        dict(item) for item in resolution.ledger_ops
                    ],
                    "status_ops": [
                        dict(item) for item in resolution.status_ops
                    ],
                    "assist_ops": [
                        dict(item) for item in resolution.assist_ops
                    ],
                }
                if workflow
                else None
            )
            try:
                experience = normalize_chat_experience(world)
                checkpoint_interval = int(
                    experience["continuity"].get("checkpoint_every_turns") or 0
                )
                updated_session = await self.database.commit_turn(
                    session_id=session_id,
                    expected_revision=session["revision"],
                    player_id=player["id"],
                    player_user_id=sender_id,
                    player_name=(
                        player["character_name"] or player["display_name"]
                    ),
                    player_input=player_input,
                    narrative=narrative,
                    world_state=new_state,
                    memories=normalized_memories,
                    check_payload=check_payload,
                    model_payload={**dict(resolution.raw), "_quality": quality},
                    director_note=resolution.director_note,
                    auto_snapshot_interval=(
                        checkpoint_interval
                        if experience.get("enabled") and checkpoint_interval > 0
                        else config.auto_snapshot_interval
                    ),
                    store_model_payload=config.store_model_payloads,
                    workflow=commit_workflow,
                    # 0.11.1：回执完成并入提交事务，避免已提交回合
                    # 因跨事务崩溃被永久误判为“处理中”。
                    operation_id=turn_operation_id,
                    operation_result={
                        "turn_no": operation_turn,
                        "quality": quality,
                    },
                )
            except DatabaseConflictError as exc:
                raise TavernBusyError(
                    "本轮状态刚被其他操作更新，请重新提交行动"
                ) from exc

            await self.broker.publish(
                {
                    "type": "turn",
                    "hook": "story_generated",
                    "session_id": session_id,
                    "group_id": updated_session["group_id"],
                    "turn_no": updated_session["turn_no"],
                    "actor": sender_name,
                    "checked": bool(dice),
                }
            )
            next_turn = await self.database.get_turn_status(session_id)
            story_body = self._format_story_paragraphs(narrative)
            story_output = "📖 【故事推进】\n\n" + story_body
            current_name = (
                next_turn["current_name"]
                or next_turn["current_user_id"]
                or "等待玩家加入"
            )
            workflow_result = (
                updated_session.get("workflow", {})
                if commit_workflow
                else {}
            )
            vote_pending = bool(workflow_result.get("vote_id"))
            if vote_pending:
                turn_footer = (
                    f"⚔️ 【回合秩序】{current_name} 的行动权已挂起 · "
                    "集体投票不消耗本次机会"
                )
            elif len(next_turn["order"]) > 1:
                if next_turn["round_no"] > acting_round:
                    turn_footer = (
                        f"⚔️ 【回合秩序】第 {acting_round} 轮结束 · "
                        f"第 {next_turn['round_no']} 轮：{current_name}"
                    )
                else:
                    turn_footer = (
                        f"⚔️ 【回合秩序】第 {acting_round} 轮 · "
                        f"下一位：{current_name}"
                    )
            else:
                turn_footer = (
                    f"⚔️ 【回合秩序】第 {next_turn['round_no']} 轮 · "
                    f"当前：{current_name}"
                )
            turn_output = turn_footer
            if commit_workflow:
                world_event = workflow_result.get("world_event")
                if world_event:
                    turn_output += (
                        "\n\n🌐 【世界脉冲】"
                        f"{world_event.get('title') or '局势变化'}\n"
                        f"{world_event.get('description')}"
                    )
                vote_id = workflow_result.get("vote_id")
                if vote_id:
                    vote = await self.database.active_vote(session_id)
                    if vote:
                        vote_lines = [
                            "🗳️ 【集体决策】",
                            vote["question"],
                            *[
                                f"{item.get('key')}. {item.get('text')}"
                                for item in vote["options"]
                            ],
                            "",
                            "💬 发送：/酒馆 投票 A",
                            "投票期间不消耗当前玩家的行动机会。",
                        ]
                        turn_output += "\n\n" + "\n".join(vote_lines)
                else:
                    next_choice = await self.database.active_choice_set(
                        session_id
                    )
                    if next_choice and next_choice.get("participant"):
                        next_actor = next_choice["participant"]
                        turn_output += "\n\n" + format_choices(
                            next_actor["character_name"]
                            or next_actor["display_name"],
                            next_choice["choices"],
                            rerolls_left=(
                                1 - int(next_choice["reroll_count"])
                            ),
                        )
            return EngineReply(
                text=f"{story_output}\n\n{turn_output}",
                session=updated_session,
                dice=dice,
                turn=next_turn,
                story_text=story_output,
                turn_text=turn_output,
            )

    async def _apply_economy_ops(
        self,
        *,
        session_id: str,
        ops: Any,
        operation_prefix: str,
        actor_id: str,
        source: str = "story",
    ) -> list[dict[str, Any]]:
        """A16：应用模型/世界包提议的可选经济操作（未启用则忽略，不阻断回合）。"""
        if not isinstance(ops, list) or not ops:
            return []
        try:
            state = await self.database.economy_state(session_id)
        except Exception:
            return []
        if not state.get("enabled"):
            return []
        results: list[dict[str, Any]] = []
        for index, op in enumerate(ops):
            if not isinstance(op, Mapping):
                continue
            try:
                result = await self.database.economy_apply(
                    session_id=session_id,
                    operation_id=f"{operation_prefix}:econ:{index}",
                    kind=str(op.get("kind") or "adjust"),
                    currency_id=str(op.get("currency_id") or ""),
                    amount=op.get("amount"),
                    from_owner=_owner_tuple(
                        op.get("from_owner_type"), op.get("from_owner_ref")
                    ),
                    to_owner=_owner_tuple(
                        op.get("to_owner_type"), op.get("to_owner_ref")
                    ),
                    reason=str(op.get("reason") or ""),
                    source=source,
                    actor_id=actor_id,
                    target_ref=str(op.get("target_ref") or ""),
                )
            except Exception as exc:  # noqa: BLE001 - 经济失败不阻断叙事
                logger.warning("AI 酒馆经济操作失败：%s", exc)
                result = {"ok": False, "message": str(exc)}
            results.append(result)
        return results

    def _normalize_state_patch_relationships(
        self,
        state_patch: Mapping[str, Any],
        roster: Any,
    ) -> dict[str, Any]:
        """A16：把 relationship_ops 的 source/target 规范化为稳定引用，
        避免模型输出裸 UUID 后关系键无法解析（配合统一实体解析器）。"""
        if not isinstance(state_patch, Mapping):
            return dict(state_patch or {})
        ops = state_patch.get("relationship_ops")
        if not isinstance(ops, list) or not ops:
            return dict(state_patch)
        labels = build_participant_labels(roster)
        normalized = normalize_relationship_ops(ops, labels)
        return {**dict(state_patch), "relationship_ops": normalized}

    async def _ensure_next_choices(
        self,
        *,
        resolution: Resolution,
        provider_ids: Sequence[str],
        world: Mapping[str, Any],
        session: Mapping[str, Any],
        participant: Mapping[str, Any],
        roster: Sequence[Mapping[str, Any]],
        events: Sequence[Mapping[str, Any]],
        candidate_state: Mapping[str, Any],
        config: TavernConfig,
    ) -> Resolution:
        raw_choices = resolution.raw.get("next_choices")
        validation_error = str(
            resolution.raw.get("_next_choices_error") or ""
        )
        if resolution.next_choices:
            try:
                choices = self._validate_choices_for_actor(
                    resolution.next_choices,
                    expected_actor=participant,
                    roster=roster,
                )
                return replace(
                    resolution,
                    next_choices=tuple(choices),
                )
            except (TypeError, ValueError) as exc:
                # A16：actor_id 与下一位行动角色不一致时不再硬失败，
                # 进入专用修复/兜底路径（避免“正在生成”后误报身份错误）。
                validation_error = str(exc)
                logger.warning(
                    "AI 酒馆选项 actor_id 校验失败，进入修复：%s", exc
                )

        if raw_choices is not None:
            try:
                choices = normalize_choices_compat(raw_choices, world)
                choices = self._validate_choices_for_actor(
                    choices,
                    expected_actor=participant,
                    roster=roster,
                )
                return replace(
                    resolution,
                    next_choices=tuple(choices),
                )
            except (TypeError, ValueError) as exc:
                validation_error = str(exc)
        elif not validation_error:
            validation_error = "模型未提供 next_choices"

        avoid = (
            [
                dict(item)
                for item in raw_choices
                if isinstance(item, Mapping)
            ]
            if isinstance(raw_choices, Sequence)
            and not isinstance(raw_choices, (str, bytes))
            else []
        )
        choice_session = dict(session)
        choice_session["world_state"] = dict(candidate_state)
        recovery_method = "model"
        try:
            choices = await self._generate_choices(
                provider_ids=provider_ids,
                world=world,
                session=choice_session,
                participant=participant,
                roster=roster,
                events=events,
                config=config,
                avoid=avoid,
                request_type="story_choices",
                validation_error=validation_error,
                story_context=resolution.narrative,
            )
        except TavernEngineError as exc:
            recovery_method = "fallback"
            logger.warning(
                "AI 酒馆选项专用修复失败，已使用安全兜底："
                "session=%s initial_error=%s repair_error=%s",
                session.get("id") or "",
                validation_error,
                exc,
            )
            choices = fallback_choices(candidate_state, world)
            expected_actor_id = str(
                participant.get("id")
                or participant.get("participant_id")
                or ""
            )
            for choice in choices:
                choice["actor_id"] = expected_actor_id
            choices = self._validate_choices_for_actor(
                choices,
                expected_actor=participant,
                roster=roster,
            )

        raw_payload = dict(resolution.raw)
        raw_payload["next_choices"] = [dict(item) for item in choices]
        raw_payload["_choice_recovery"] = {
            "method": recovery_method,
            "validation_error": validation_error,
        }
        return replace(
            resolution,
            next_choices=tuple(choices),
            raw=raw_payload,
        )

    async def _generate_choices(
        self,
        *,
        provider_ids: Sequence[str],
        world: Mapping[str, Any],
        session: Mapping[str, Any],
        participant: Mapping[str, Any],
        events: Sequence[Mapping[str, Any]],
        config: TavernConfig,
        avoid: Sequence[Mapping[str, Any]] = (),
        roster: Sequence[Mapping[str, Any]] = (),
        request_type: str = "choice_reroll",
        validation_error: str = "",
        story_context: str = "",
    ) -> list[dict[str, Any]]:
        prompt = choice_generation_prompt(
            world=world,
            session=session,
            participant=participant,
            events=events,
            avoid=avoid,
            validation_error=validation_error,
            story_context=story_context,
        )
        choice_system = choice_system_prompt(world)
        failures: list[str] = []
        attempts = config.json_repair_attempts + 1
        total_attempts = 0
        for provider_id in provider_ids:
            current_prompt = prompt
            last_error = ""
            timed_out = False
            for attempt in range(attempts):
                if total_attempts >= _MAX_TOTAL_MODEL_ATTEMPTS:
                    failures.append(
                        f"{provider_id}：达到全局模型重试上限"
                    )
                    timed_out = True
                    break
                total_attempts += 1
                try:
                    response = await asyncio.wait_for(
                        self._llm_generate_metered(
                            session_id=str(session.get("id") or ""),
                            request_type=(
                                request_type
                                if attempt == 0
                                else request_type + "_repair"
                            ),
                            provider_id=provider_id,
                            prompt=current_prompt,
                            system_prompt_value=choice_system,
                            temperature=config.temperature,
                            max_tokens=min(config.max_tokens, 1200),
                        ),
                        timeout=config.request_timeout_seconds,
                    )
                except TimeoutError:
                    failures.append(f"{provider_id}：请求超时")
                    await self.database.record_provider_result(
                        provider_id,
                        success=False,
                        reason="选项生成请求超时",
                    )
                    timed_out = True
                    break
                except Exception as exc:
                    failures.append(
                        f"{provider_id}：{type(exc).__name__}"
                    )
                    await self.database.record_provider_result(
                        provider_id,
                        success=False,
                        reason=(
                            "选项生成调用失败："
                            f"{type(exc).__name__}: {exc}"
                        ),
                    )
                    timed_out = True
                    break
                raw = str(
                    getattr(response, "completion_text", "") or ""
                )
                try:
                    payload = extract_json_object(raw)
                    choices = normalize_choices_compat(
                        payload.get("choices", payload.get("next_choices")), world
                    )
                    choices = self._validate_choices_for_actor(
                        choices,
                        expected_actor=participant,
                        roster=roster,
                    )
                    await self.database.record_provider_result(
                        provider_id,
                        success=True,
                    )
                    return choices
                except (TypeError, ValueError) as exc:
                    last_error = str(exc)
                    if attempt + 1 >= attempts:
                        break
                    current_prompt = choice_repair_prompt(
                        raw,
                        last_error,
                        world=world,
                        participant=participant,
                    )
            if not timed_out:
                failures.append(
                    f"{provider_id}：结构校验失败"
                    f"（{last_error or '未知错误'}）"
                )
                await self.database.record_provider_result(
                    provider_id,
                    success=False,
                    reason=(
                        "选项生成结构校验失败："
                        f"{last_error or '未知错误'}"
                    ),
                )
        raise TavernEngineError(
            "未能生成一组合法的新选项："
            + ("；".join(failures) or "没有可用模型")
        )

    async def _generate_resolution(
        self,
        *,
        session_id: str,
        request_type: str,
        world: Mapping[str, Any],
        provider_ids: list[str],
        system: str,
        prompt: str,
        config: TavernConfig,
        expected_actor: Mapping[str, Any] | None = None,
        roster: Sequence[Mapping[str, Any]] = (),
        enforce_mobile_limits: bool = False,
    ) -> tuple[Resolution, str]:
        attempts = config.json_repair_attempts + 1
        original_prompt = prompt
        failures: list[str] = []
        total_attempts = 0
        for provider_id in provider_ids:
            current_prompt = prompt
            last_error = ""
            provider_failed = False
            for attempt in range(attempts):
                if total_attempts >= _MAX_TOTAL_MODEL_ATTEMPTS:
                    failures.append(
                        f"{provider_id}：达到全局模型重试上限"
                    )
                    provider_failed = True
                    break
                total_attempts += 1
                try:
                    response = await asyncio.wait_for(
                        self._llm_generate_metered(
                            session_id=session_id,
                            request_type=(
                                request_type
                                if attempt == 0
                                else request_type + "_repair"
                            ),
                            provider_id=provider_id,
                            prompt=current_prompt,
                            system_prompt_value=system,
                            temperature=config.temperature,
                            max_tokens=config.max_tokens,
                        ),
                        timeout=config.request_timeout_seconds,
                    )
                except TimeoutError:
                    failures.append(f"{provider_id}：请求超时")
                    await self.database.record_provider_result(
                        provider_id,
                        success=False,
                        reason="叙事请求超时",
                    )
                    provider_failed = True
                    break
                except Exception as exc:
                    failures.append(
                        f"{provider_id}：{type(exc).__name__}"
                    )
                    await self.database.record_provider_result(
                        provider_id,
                        success=False,
                        reason=f"叙事调用失败：{type(exc).__name__}",
                    )
                    provider_failed = True
                    break

                raw = str(
                    getattr(response, "completion_text", "") or ""
                )
                try:
                    payload = extract_json_object(raw)
                    core_payload = dict(payload)
                    raw_choices = core_payload.pop("next_choices", None)
                    resolution = validate_resolution(core_payload)
                    if enforce_mobile_limits:
                        resolution = self._validate_mobile_resolution(
                            resolution,
                            expected_actor=expected_actor,
                            roster=roster,
                        )
                    choice_error = ""
                    normalized_choices: list[dict[str, Any]] = []
                    if resolution.mode == "resolve" and raw_choices is not None:
                        try:
                            normalized_choices = normalize_choices_compat(
                                raw_choices, world
                            )
                            if enforce_mobile_limits:
                                normalized_choices = (
                                    self._validate_choices_for_actor(
                                        normalized_choices,
                                        expected_actor=expected_actor,
                                        roster=roster,
                                    )
                                )
                        except (TypeError, ValueError) as exc:
                            choice_error = str(exc)
                    raw_payload = dict(payload)
                    if choice_error:
                        raw_payload["_next_choices_error"] = choice_error
                    resolution = replace(
                        resolution,
                        next_choices=tuple(normalized_choices),
                        raw=raw_payload,
                    )
                    await self.database.record_provider_result(
                        provider_id,
                        success=True,
                    )
                    return resolution, provider_id
                except (TypeError, ValueError) as exc:
                    last_error = str(exc)
                    if attempt + 1 >= attempts:
                        break
                    current_prompt = repair_prompt(
                        raw,
                        last_error,
                        original_prompt,
                    )
            if not provider_failed:
                await self.database.record_provider_result(
                    provider_id,
                    success=False,
                    reason=f"结构校验失败：{last_error or '未知错误'}",
                )
                failures.append(
                    f"{provider_id}：结构校验失败"
                    f"（{last_error or '未知错误'}）"
                )

        summary = "；".join(failures) or "没有可用模型"
        raise TavernEngineError(
            f"全部叙事模型均未完成本轮：{summary}"
        )
