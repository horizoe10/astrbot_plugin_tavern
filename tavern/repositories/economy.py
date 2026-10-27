"""A16：可选多货币经济系统领域仓库（世界包 economy 块驱动，默认关闭）。

设计要点：
- 完全可选：未启用或世界包未声明 `rules.economy` 时，任何调用都返回「未启用」，
  不自动创建货币/钱包，不干扰旧副本。
- 稳定 ID：钱包绑定 owner_type + owner_ref（稳定 ID），不绑定显示名称。
- 金额以「最小单位整数」存储（精度由货币定义决定），避免浮点漂移。
- 幂等：`operation_id` 全局唯一；重复提交返回首次结果，杜绝重复扣款/重复到账。
- 并发：所有写操作在 `BEGIN IMMEDIATE` 单事务内完成，余额读改写互斥。
- 标准化结果：成功/失败都返回 {ok, operation_id, currency_id, amount,
  balance_before, balance_after, reason, actor_id, created_at, ...}。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from astrbot.api import logger

from ..database_support import new_id, json_dump, json_load, utc_now
from ..errors import EconomyDisabledError, InsufficientFundsError

ECONOMY_SOURCE_STORY = "story"
ECONOMY_SOURCE_ADMIN = "admin"
ECONOMY_SOURCE_DM = "dm"
ECONOMY_SOURCE_WEB = "web"
ECONOMY_SOURCE_WORLD = "world"


def _major_to_minor(value: Any, precision: int) -> int:
    """把主单位金额（str/int/float/Decimal）转为最小单位整数。"""
    try:
        dec = Decimal(str(value or "0")).quantize(
            Decimal(1).scaleb(-int(precision))
        )
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"金额格式无效：{value}") from None
    return int(dec.scaleb(int(precision)))


def _minor_to_major(value: int, precision: int) -> str:
    if not int(precision):
        return str(int(value))
    return format(Decimal(int(value)).scaleb(-int(precision)), "f")


class EconomyRepositoryMixin:
    # ── 开关与能力 ────────────────────────────────────────────────
    async def set_economy_enabled(
        self,
        session_id: str,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        result = await self._run(
            self._set_economy_enabled, session_id, bool(enabled), actor_id
        )
        if enabled:
            try:
                await self.ensure_economy_currencies(session_id)
            except Exception:
                logger.exception("AI 酒馆启用经济后初始化货币失败")
        return result

    def _set_economy_enabled(
        self,
        session_id: str,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO economy_state(session_id, enabled, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        enabled = excluded.enabled,
                        updated_at = excluded.updated_at
                    """,
                    (session_id, int(enabled), now),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "economy.enabled",
                    "",
                    {"enabled": enabled},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"session_id": session_id, "enabled": enabled}

    async def economy_state(self, session_id: str) -> dict[str, Any]:
        return await self._run(self._economy_state, session_id)

    def _economy_state(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT enabled FROM economy_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return {"session_id": session_id, "enabled": bool(row and row["enabled"])}

    def _require_enabled(self, connection: Any, session_id: str) -> None:
        row = connection.execute(
            "SELECT enabled FROM economy_state WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row or not row["enabled"]:
            raise EconomyDisabledError("经济系统未启用")

    def _currency_precision(
        self,
        connection: Any,
        session_id: str,
        currency_id: str,
    ) -> int:
        row = connection.execute(
            """
            SELECT precision FROM economy_currencies
            WHERE session_id = ? AND currency_id = ?
            """,
            (session_id, currency_id),
        ).fetchone()
        return int(row["precision"]) if row else 0

    def _ensure_wallet(
        self,
        connection: Any,
        session_id: str,
        owner_type: str,
        owner_ref: str,
        currency_id: str,
        *,
        initial: int = 0,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO economy_wallets(
                id, session_id, owner_type, owner_ref, currency_id,
                balance, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("wallet"),
                session_id,
                str(owner_type or "party"),
                str(owner_ref or ""),
                currency_id,
                int(initial),
                utc_now(),
            ),
        )

    # ── 货币定义（懒播种） ────────────────────────────────────────
    async def ensure_economy_currencies(self, session_id: str) -> int:
        return await self._run(self._ensure_economy_currencies, session_id)

    def _ensure_economy_currencies(self, session_id: str) -> int:
        """从冻结世界快照 `rules.economy` 播种货币与兑换规则（幂等）。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT world_snapshot_json FROM instance_configs WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return 0
            world = json_load(row["world_snapshot_json"], {})
        rules = world.get("rules") if isinstance(world, Mapping) else {}
        economy = rules.get("economy") if isinstance(rules, Mapping) else {}
        if not isinstance(economy, Mapping) or not economy:
            return 0
        currencies = economy.get("currencies") or []
        if not isinstance(currencies, list):
            return 0
        now = utc_now()
        created = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = {
                    str(r[0])
                    for r in connection.execute(
                        "SELECT currency_id FROM economy_currencies WHERE session_id = ?",
                        (session_id,),
                    ).fetchall()
                }
                for index, raw in enumerate(currencies):
                    if not isinstance(raw, Mapping):
                        continue
                    currency_id = str(
                        raw.get("currency_id") or raw.get("id") or ""
                    ).strip()
                    name = str(raw.get("name") or currency_id).strip()
                    if not currency_id or currency_id in existing:
                        continue
                    connection.execute(
                        """
                        INSERT INTO economy_currencies(
                            id, session_id, currency_id, name, short_name, icon,
                            description, precision, allow_negative, transferable,
                            exchangeable, public, sort_order, extensions_json,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_id("ecur"),
                            session_id,
                            currency_id,
                            name,
                            str(raw.get("short_name") or "").strip(),
                            str(raw.get("icon") or "").strip(),
                            str(raw.get("description") or "").strip(),
                            int(raw.get("precision") or 0),
                            int(bool(raw.get("allow_negative", False))),
                            int(bool(raw.get("transferable", True))),
                            int(bool(raw.get("exchangeable", False))),
                            int(bool(raw.get("public", True))),
                            int(raw.get("sort_order") or index),
                            json_dump(raw.get("extensions") or {}),
                            now,
                        ),
                    )
                    created += 1
                    existing.add(currency_id)
                wallets = economy.get("initial_wallets") or []
                if isinstance(wallets, list):
                    for item in wallets:
                        if not isinstance(item, Mapping):
                            continue
                        owner_type = str(item.get("owner_type") or "party")
                        owner_ref = str(item.get("owner_ref") or "").strip()
                        cid = str(item.get("currency_id") or "").strip()
                        if not owner_ref or not cid:
                            continue
                        precision = self._currency_precision(
                            connection, session_id, cid
                        )
                        self._ensure_wallet(
                            connection,
                            session_id,
                            owner_type,
                            owner_ref,
                            cid,
                            initial=_major_to_minor(
                                item.get("amount") or 0, precision
                            ),
                        )
                rules_list = economy.get("exchange_rules") or []
                if isinstance(rules_list, list):
                    for item in rules_list:
                        if not isinstance(item, Mapping):
                            continue
                        frm = str(item.get("from") or item.get("from_currency") or "")
                        to = str(item.get("to") or item.get("to_currency") or "")
                        if not frm or not to:
                            continue
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO economy_exchange_rules(
                                id, session_id, from_currency, to_currency,
                                rate_numerator, rate_denominator, fee, enabled,
                                created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                            """,
                            (
                                new_id("exch"),
                                session_id,
                                frm,
                                to,
                                int(item.get("rate_numerator") or 1),
                                int(item.get("rate_denominator") or 1),
                                int(item.get("fee") or 0),
                                now,
                            ),
                        )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return created

    # ── 查询 ──────────────────────────────────────────────────────
    async def economy_summary(self, session_id: str) -> dict[str, Any]:
        return await self._run(self._economy_summary, session_id)

    def _economy_summary(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            state = self._economy_state(session_id)
            currencies = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT currency_id, name, short_name, icon, precision,
                           allow_negative, transferable, exchangeable, public,
                           sort_order, extensions_json
                    FROM economy_currencies WHERE session_id = ?
                    ORDER BY sort_order, currency_id
                    """,
                    (session_id,),
                ).fetchall()
            ]
            wallets = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT owner_type, owner_ref, currency_id, balance
                    FROM economy_wallets WHERE session_id = ?
                    ORDER BY owner_type, owner_ref, currency_id
                    """,
                    (session_id,),
                ).fetchall()
            ]
            recent = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT operation_id, kind, currency_id, from_owner_type,
                           from_owner_ref, to_owner_type, to_owner_ref, amount,
                           balance_before, balance_after, reason, source,
                           actor_id, status, created_at
                    FROM economy_transactions WHERE session_id = ?
                    ORDER BY created_at DESC, rowid DESC LIMIT 20
                    """,
                    (session_id,),
                ).fetchall()
            ]
            rules = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT from_currency, to_currency, rate_numerator,
                           rate_denominator, fee, enabled
                    FROM economy_exchange_rules WHERE session_id = ?
                    ORDER BY from_currency, to_currency
                    """,
                    (session_id,),
                ).fetchall()
            ]
        return {
            "enabled": state["enabled"],
            "currencies": currencies,
            "wallets": wallets,
            "recent": recent,
            "exchange_rules": rules,
        }

    async def economy_balance(
        self,
        session_id: str,
        owner_type: str,
        owner_ref: str,
        currency_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._economy_balance,
            session_id,
            owner_type,
            owner_ref,
            currency_id,
        )

    def _economy_balance(
        self,
        session_id: str,
        owner_type: str,
        owner_ref: str,
        currency_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            self._require_enabled(connection, session_id)
            row = connection.execute(
                """
                SELECT balance FROM economy_wallets
                WHERE session_id = ? AND owner_type = ? AND owner_ref = ?
                  AND currency_id = ?
                """,
                (session_id, owner_type, owner_ref, currency_id),
            ).fetchone()
            precision = self._currency_precision(connection, session_id, currency_id)
        return {
            "ok": True,
            "session_id": session_id,
            "owner_type": owner_type,
            "owner_ref": owner_ref,
            "currency_id": currency_id,
            "balance": int(row["balance"]) if row else 0,
            "balance_major": _minor_to_major(
                int(row["balance"]) if row else 0, precision
            ),
        }

    # ── 核心事务操作 ──────────────────────────────────────────────
    async def economy_apply(
        self,
        *,
        session_id: str,
        operation_id: str,
        kind: str,
        currency_id: str,
        amount: Any,
        from_owner: tuple[str, str] | None = None,
        to_owner: tuple[str, str] | None = None,
        reason: str = "",
        source: str = ECONOMY_SOURCE_WEB,
        actor_id: str = "",
        target_ref: str = "",
        event_id: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._economy_apply,
            session_id,
            str(operation_id or "").strip(),
            str(kind or "adjust").strip(),
            str(currency_id or "").strip(),
            amount,
            from_owner,
            to_owner,
            str(reason or "").strip(),
            str(source or "").strip(),
            str(actor_id or "").strip(),
            str(target_ref or "").strip(),
            str(event_id or "").strip(),
        )

    def _economy_apply(
        self,
        session_id: str,
        operation_id: str,
        kind: str,
        currency_id: str,
        amount: Any,
        from_owner: tuple[str, str] | None,
        to_owner: tuple[str, str] | None,
        reason: str,
        source: str,
        actor_id: str,
        target_ref: str,
        event_id: str,
    ) -> dict[str, Any]:
        if not operation_id:
            raise ValueError("经济操作必须提供 operation_id（幂等键）")
        if not currency_id:
            raise ValueError("缺少货币 currency_id")
        if not from_owner and not to_owner:
            raise ValueError("经济操作至少需要一个钱包方向")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_enabled(connection, session_id)
                existing = connection.execute(
                    """
                    SELECT * FROM economy_transactions
                    WHERE session_id = ? AND operation_id = ?
                    """,
                    (session_id, operation_id),
                ).fetchone()
                if existing:
                    connection.execute("COMMIT")
                    return self._tx_result(existing)
                precision = self._currency_precision(
                    connection, session_id, currency_id
                )
                minor = _major_to_minor(amount, precision)
                if minor == 0:
                    raise ValueError("经济操作金额不能为 0")
                currency = connection.execute(
                    """
                    SELECT allow_negative, transferable FROM economy_currencies
                    WHERE session_id = ? AND currency_id = ?
                    """,
                    (session_id, currency_id),
                ).fetchone()
                if not currency:
                    raise ValueError(f"货币未定义：{currency_id}")

                def wallet_balance(owner: tuple[str, str] | None) -> int:
                    if not owner:
                        return 0
                    row = connection.execute(
                        """
                        SELECT balance FROM economy_wallets
                        WHERE session_id = ? AND owner_type = ? AND owner_ref = ?
                          AND currency_id = ?
                        """,
                        (session_id, owner[0], owner[1], currency_id),
                    ).fetchone()
                    return int(row["balance"]) if row else 0

                from_before = from_after = 0
                if from_owner:
                    self._ensure_wallet(
                        connection, session_id, from_owner[0], from_owner[1],
                        currency_id,
                    )
                    from_before = wallet_balance(from_owner)
                    from_after = from_before - minor
                    if from_after < 0 and not currency["allow_negative"]:
                        raise InsufficientFundsError(f"{currency_id} 余额不足")
                    connection.execute(
                        """
                        UPDATE economy_wallets SET balance = ?, updated_at = ?
                        WHERE session_id = ? AND owner_type = ? AND owner_ref = ?
                          AND currency_id = ?
                        """,
                        (
                            from_after, utc_now(), session_id,
                            from_owner[0], from_owner[1], currency_id,
                        ),
                    )
                to_before = to_after = 0
                if to_owner:
                    self._ensure_wallet(
                        connection, session_id, to_owner[0], to_owner[1],
                        currency_id,
                    )
                    to_before = wallet_balance(to_owner)
                    to_after = to_before + minor
                    connection.execute(
                        """
                        UPDATE economy_wallets SET balance = ?, updated_at = ?
                        WHERE session_id = ? AND owner_type = ? AND owner_ref = ?
                          AND currency_id = ?
                        """,
                        (
                            to_after, utc_now(), session_id,
                            to_owner[0], to_owner[1], currency_id,
                        ),
                    )
                now = utc_now()
                tx_id = new_id("etx")
                connection.execute(
                    """
                    INSERT INTO economy_transactions(
                        id, session_id, operation_id, kind, currency_id,
                        from_owner_type, from_owner_ref, to_owner_type,
                        to_owner_ref, amount, balance_before, balance_after,
                        reason, source, actor_id, target_ref, event_id,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'committed', ?)
                    """,
                    (
                        tx_id, session_id, operation_id, kind, currency_id,
                        (from_owner[0] if from_owner else ""),
                        (from_owner[1] if from_owner else ""),
                        (to_owner[0] if to_owner else ""),
                        (to_owner[1] if to_owner else ""),
                        minor, from_before, from_after,
                        reason, source, actor_id, target_ref, event_id, now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id or source,
                    "economy.apply",
                    tx_id,
                    {
                        "operation_id": operation_id,
                        "kind": kind,
                        "currency_id": currency_id,
                        "amount": minor,
                        "from": from_owner,
                        "to": to_owner,
                        "source": source,
                    },
                )
                connection.execute("COMMIT")
                return {
                    "ok": True,
                    "operation_id": operation_id,
                    "transaction_id": tx_id,
                    "session_id": session_id,
                    "kind": kind,
                    "currency_id": currency_id,
                    "amount": minor,
                    "amount_major": _minor_to_major(minor, precision),
                    "balance_before": from_before,
                    "balance_after": from_after,
                    "actor_id": actor_id,
                    "created_at": now,
                }
            except InsufficientFundsError:
                connection.execute("ROLLBACK")
                return {
                    "ok": False,
                    "operation_id": operation_id,
                    "session_id": session_id,
                    "kind": kind,
                    "currency_id": currency_id,
                    "reason": "insufficient_funds",
                    "message": f"{currency_id} 余额不足",
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _tx_result(self, row: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "operation_id": row["operation_id"],
            "transaction_id": row["id"],
            "session_id": row["session_id"],
            "kind": row["kind"],
            "currency_id": row["currency_id"],
            "amount": row["amount"],
            "balance_before": row["balance_before"],
            "balance_after": row["balance_after"],
            "actor_id": row["actor_id"],
            "created_at": row["created_at"],
            "idempotent_replay": True,
        }

    async def economy_transfer(
        self,
        *,
        session_id: str,
        operation_id: str,
        currency_id: str,
        from_owner: tuple[str, str],
        to_owner: tuple[str, str],
        amount: Any,
        reason: str = "",
        source: str = ECONOMY_SOURCE_WEB,
        actor_id: str = "",
    ) -> dict[str, Any]:
        return await self.economy_apply(
            session_id=session_id,
            operation_id=operation_id,
            kind="transfer",
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
        *,
        session_id: str,
        operation_id: str,
        currency_id: str,
        from_owner: tuple[str, str],
        to_owner: tuple[str, str, str],
        amount: Any,
        source: str = ECONOMY_SOURCE_WEB,
        actor_id: str = "",
    ) -> dict[str, Any]:
        """按兑换规则把 amount 主单位的 from 货币换成 to 货币（受汇率约束）。"""
        plan = await self._run(
            self._economy_exchange_plan,
            session_id,
            str(operation_id or "").strip(),
            str(currency_id or "").strip(),
            from_owner,
            to_owner,
            amount,
        )
        if plan.get("replay"):
            return plan
        to_currency = plan["to_currency"]
        from_minor = plan["from_minor"]
        to_minor = plan["to_minor"]
        from_precision = plan["from_precision"]
        to_precision = plan["to_precision"]
        numerator = plan["numerator"]
        denominator = plan["denominator"]
        fee = plan["fee"]
        debit = await self.economy_apply(
            session_id=session_id,
            operation_id=f"{operation_id}:debit",
            kind="exchange",
            currency_id=currency_id,
            amount=_minor_to_major(from_minor, from_precision),
            from_owner=(from_owner[0], from_owner[1]),
            reason="exchange",
            source=source,
            actor_id=actor_id,
        )
        if not debit.get("ok"):
            return debit
        credit = await self.economy_apply(
            session_id=session_id,
            operation_id=f"{operation_id}:credit",
            kind="exchange",
            currency_id=to_currency,
            amount=_minor_to_major(to_minor, to_precision),
            to_owner=(to_owner[0], to_owner[1]),
            reason="exchange",
            source=source,
            actor_id=actor_id,
        )
        if not credit.get("ok"):
            await self.economy_apply(
                session_id=session_id,
                operation_id=f"{operation_id}:refund",
                kind="exchange_refund",
                currency_id=currency_id,
                amount=_minor_to_major(from_minor, from_precision),
                to_owner=(from_owner[0], from_owner[1]),
                reason="exchange_refund",
                source=source,
                actor_id=actor_id,
            )
            return {
                **credit,
                "message": "兑换入账失败，已退回来源："
                + str(credit.get("message", "")),
            }
        return {
            **credit,
            "from_currency": currency_id,
            "to_currency": to_currency,
            "from_amount": _minor_to_major(from_minor, from_precision),
            "to_amount": _minor_to_major(to_minor, to_precision),
            "rate": f"{numerator}/{denominator}",
            "fee": fee,
        }

    def _economy_exchange_plan(
        self,
        session_id: str,
        operation_id: str,
        currency_id: str,
        from_owner: tuple[str, str],
        to_owner: tuple[str, str, str],
        amount: Any,
    ) -> dict[str, Any]:
        to_currency = to_owner[2] if len(to_owner) >= 3 else ""
        if not to_currency:
            raise ValueError("兑换必须指定目标货币")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_enabled(connection, session_id)
                existing = connection.execute(
                    """
                    SELECT * FROM economy_transactions
                    WHERE session_id = ? AND operation_id = ?
                    """,
                    (session_id, operation_id),
                ).fetchone()
                if existing:
                    connection.execute("COMMIT")
                    return self._tx_result(existing)
                rule = connection.execute(
                    """
                    SELECT rate_numerator, rate_denominator, fee, enabled
                    FROM economy_exchange_rules
                    WHERE session_id = ? AND from_currency = ? AND to_currency = ?
                    """,
                    (session_id, currency_id, to_currency),
                ).fetchone()
                if not rule or not rule["enabled"]:
                    raise ValueError(
                        f"该货币不可兑换：{currency_id} → {to_currency}"
                    )
                from_precision = self._currency_precision(
                    connection, session_id, currency_id
                )
                to_precision = self._currency_precision(
                    connection, session_id, to_currency
                )
                from_minor = _major_to_minor(amount, from_precision)
                numerator = int(rule["rate_numerator"])
                denominator = int(rule["rate_denominator"] or 1)
                # 汇率按主单位结算，再换算到目标货币的最小单位
                from_major = Decimal(from_minor) / Decimal(10 ** from_precision)
                to_major = (
                    from_major * Decimal(numerator) / Decimal(denominator)
                )
                to_minor = int(to_major * Decimal(10 ** to_precision))
                fee = int(rule["fee"] or 0)
                to_minor = max(0, to_minor - fee)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {
            "to_currency": to_currency,
            "from_minor": from_minor,
            "to_minor": to_minor,
            "from_precision": from_precision,
            "to_precision": to_precision,
            "numerator": numerator,
            "denominator": denominator,
            "fee": fee,
        }

    async def economy_list_transactions(
        self,
        session_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._economy_list_transactions, session_id, int(limit)
        )

    def _economy_list_transactions(
        self,
        session_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        limit = min(500, max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM economy_transactions
                WHERE session_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]
