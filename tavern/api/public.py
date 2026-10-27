from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..diagnostics import build_diagnostic_report, redact
from ..presets import (
    check_character_knowledge,
    check_content_permission,
    explain_boundary_sources,
    normalize_preset_dimensions,
    resolve_character_presets,
)


class TavernPublicAPI:
    """Supported service facade; extensions never receive raw SQL access."""

    def __init__(self, database: Any, hooks: Any, extensions: Any, engine: Any = None) -> None:
        self._database = database
        self.hooks = hooks
        self.extensions = extensions
        self._engine = engine

    async def get_current_session(
        self,
        platform_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        return await self._database.get_session_by_group(
            platform_id,
            group_id,
        )

    async def get_turn_context(self, session_id: str) -> dict[str, Any]:
        return await self._database.get_turn_status(session_id)

    async def get_active_character(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        return await self._database.get_participant(
            session_id,
            user_id=user_id,
        )

    async def create_snapshot(
        self,
        session_id: str,
        name: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._database.create_snapshot(
            session_id,
            name,
            actor_id,
        )

    async def append_story_event(
        self,
        session_id: str,
        text: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._database.emergency_append_narrative(
            session_id,
            text,
            actor_id,
        )

    async def resolve_action_intent(
        self,
        session_id: str,
        intent: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
        *,
        dry_run: bool = True,
        operation_id: str = "",
        actor_id: str = "",
    ) -> dict[str, Any]:
        """Validate/simulate or atomically commit a protocol-v5 action."""

        return await self._database.resolve_action_intent(
            session_id,
            intent,
            context,
            dry_run=dry_run,
            operation_id=operation_id,
            actor_id=actor_id,
        )

    async def get_resolution_receipt(
        self, receipt_id: str, *, public_only: bool = True
    ) -> dict[str, Any]:
        return await self._database.get_resolution_receipt(
            receipt_id, public_only=public_only
        )


    # ── A16：可选经济系统（世界包 economy 块驱动；未启用时返回 ok=False）──
    async def economy_state(self, session_id: str) -> dict[str, Any]:
        return await self._database.economy_state(session_id)

    async def economy_enable(
        self, session_id: str, enabled: bool, actor_id: str
    ) -> dict[str, Any]:
        return await self._database.set_economy_enabled(
            session_id, enabled, actor_id
        )

    async def economy_balance(
        self, session_id: str, owner_type: str, owner_ref: str, currency_id: str
    ) -> dict[str, Any]:
        return await self._database.economy_balance(
            session_id, owner_type, owner_ref, currency_id
        )

    async def economy_credit(
        self,
        session_id: str,
        operation_id: str,
        currency_id: str,
        amount: Any,
        owner_type: str,
        owner_ref: str,
        reason: str = "",
        source: str = "api",
        actor_id: str = "",
    ) -> dict[str, Any]:
        return await self._database.economy_apply(
            session_id=session_id,
            operation_id=operation_id,
            kind="credit",
            currency_id=currency_id,
            amount=amount,
            to_owner=(owner_type, owner_ref),
            reason=reason,
            source=source,
            actor_id=actor_id,
        )

    async def economy_debit(
        self,
        session_id: str,
        operation_id: str,
        currency_id: str,
        amount: Any,
        owner_type: str,
        owner_ref: str,
        reason: str = "",
        source: str = "api",
        actor_id: str = "",
    ) -> dict[str, Any]:
        return await self._database.economy_apply(
            session_id=session_id,
            operation_id=operation_id,
            kind="debit",
            currency_id=currency_id,
            amount=amount,
            from_owner=(owner_type, owner_ref),
            reason=reason,
            source=source,
            actor_id=actor_id,
        )

    async def economy_transfer(
        self,
        session_id: str,
        operation_id: str,
        currency_id: str,
        amount: Any,
        from_owner: tuple[str, str],
        to_owner: tuple[str, str],
        reason: str = "",
        source: str = "api",
        actor_id: str = "",
    ) -> dict[str, Any]:
        return await self._database.economy_transfer(
            session_id=session_id,
            operation_id=operation_id,
            currency_id=currency_id,
            amount=amount,
            from_owner=from_owner,
            to_owner=to_owner,
            reason=reason,
            source=source,
            actor_id=actor_id,
        )

    async def economy_exchange(
        self,
        session_id: str,
        operation_id: str,
        currency_id: str,
        amount: Any,
        from_owner: tuple[str, str],
        to_owner: tuple[str, str, str],
        source: str = "api",
        actor_id: str = "",
    ) -> dict[str, Any]:
        return await self._database.economy_exchange(
            session_id=session_id,
            operation_id=operation_id,
            currency_id=currency_id,
            amount=amount,
            from_owner=from_owner,
            to_owner=to_owner,
            source=source,
            actor_id=actor_id,
        )

    async def economy_transactions(
        self, session_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        return await self._database.economy_list_transactions(session_id, limit)

    async def get_control_state(self, session_id: str) -> dict[str, Any]:
        return await self._database.get_control_state(session_id)

    async def enable_dm_mode(
        self, session_id: str, dm_user_id: str, actor_id: str
    ) -> dict[str, Any]:
        return await self._database.enable_dm_mode(session_id, dm_user_id, actor_id)

    async def set_dm_directive(
        self, session_id: str, directive: str, actor_id: str
    ) -> dict[str, Any]:
        return await self._database.set_dm_directive(session_id, directive, actor_id)

    async def generate_dm_beat(
        self, event: Any, session_id: str, instruction: str, actor_id: str
    ) -> dict[str, Any]:
        if self._engine is None:
            raise RuntimeError("当前运行时没有连接叙事引擎")
        return await self._engine.process_dm_beat(
            event=event, session_id=session_id,
            dm_user_id=actor_id, instruction=instruction,
        )

    async def append_dm_narrative(
        self, session_id: str, narrative: str, actor_id: str
    ) -> dict[str, Any]:
        session = await self._database.get_session(session_id)
        return await self._database.commit_dm_beat(
            session_id=session_id,
            expected_revision=int(session["revision"]),
            dm_user_id=actor_id,
            instruction=narrative,
            narrative=narrative,
            world_state=session["world_state"],
            direct=True,
        )

    async def handoff_to_actor(
        self, session_id: str, actor_type: str, actor_ref: str, actor_id: str
    ) -> dict[str, Any]:
        return await self._database.set_dm_handoff(
            session_id, actor_type, actor_ref, actor_id
        )

    async def return_to_auto(
        self, session_id: str, participant_ref: str, actor_id: str
    ) -> dict[str, Any]:
        participant = await self._database.get_participant(
            session_id, participant_ref=participant_ref
        )
        turn = await self._database.designate_turn(
            session_id, participant["group_user_id"], actor_id
        )
        await self._database.disable_dm_mode(session_id, actor_id)
        return {"control": await self._database.get_control_state(session_id), "turn": turn}

    async def takeover_dm(
        self, session_id: str, new_dm_user_id: str, actor_id: str
    ) -> dict[str, Any]:
        return await self._database.enable_dm_mode(
            session_id, new_dm_user_id, actor_id
        )

    @staticmethod
    def list_preset_dimensions(world: Mapping[str, Any]) -> list[dict[str, Any]]:
        rules = world.get("rules") if isinstance(world, Mapping) else {}
        rules = rules if isinstance(rules, Mapping) else {}
        card = rules.get("character_card")
        return normalize_preset_dimensions(
            card if isinstance(card, Mapping) else {}
        )

    @staticmethod
    def resolve_character_presets(
        world: Mapping[str, Any], selections: Mapping[str, Any]
    ) -> dict[str, Any]:
        return resolve_character_presets(world, selections)

    @staticmethod
    def explain_boundary_sources(
        world: Mapping[str, Any], selections: Mapping[str, Any]
    ) -> dict[str, Any]:
        return explain_boundary_sources(world, selections)

    @staticmethod
    def check_character_knowledge(
        resolved: Mapping[str, Any], domain: str
    ) -> dict[str, Any]:
        return check_character_knowledge(resolved, domain)

    @staticmethod
    def check_content_permission(
        resolved: Mapping[str, Any], content_tags: Any
    ) -> dict[str, Any]:
        return check_content_permission(resolved, content_tags)

    async def resolve_element_reaction(
        self,
        world: Mapping[str, Any],
        source_element: str,
        target_ref: str,
        target_element: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """解析一次「属性元素反应」（只读、不落库）。

        世界包需声明 ``elemental`` 块；若声明了 ``resolver``，则优先走已注册的
        ``element_resolver`` 扩展点，异常或未命中时回退声明式表。
        """
        from .elemental import parse, resolve

        parsed = parse(world)
        provider = None
        resolver_name = str(parsed.get("resolver") or "")
        if resolver_name:
            provider = self.extensions.resolve(
                "element_resolver", resolver_name
            )
        return resolve(
            parsed,
            source_element,
            target_ref,
            target_element=target_element,
            context=context,
            resolver=provider,
        )

    async def export_diagnostic(
        self,
        session_id: str,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        report = await build_diagnostic_report(self._database, session_id)
        report["extension_context"] = redact(dict(context))
        return report
