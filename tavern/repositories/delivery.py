"""Persistent delivery outbox used when an adapter cannot push proactively."""

from __future__ import annotations

from typing import Any

from ..database_support import new_id, utc_now


class DeliveryRepositoryMixin:
    async def queue_delivery(
        self,
        *,
        session_id: str,
        origin: str,
        kind: str,
        text: str,
        reason: str,
        dedupe_key: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._queue_delivery,
            session_id,
            origin,
            kind,
            text,
            reason,
            dedupe_key,
        )

    def _queue_delivery(
        self,
        session_id: str,
        origin: str,
        kind: str,
        text: str,
        reason: str,
        dedupe_key: str,
    ) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        origin = str(origin or "").strip()
        text = str(text or "").strip()
        if not origin or not text:
            raise ValueError("待投递通知必须包含会话来源与正文")
        now = utc_now()
        item_id = new_id("delivery")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if dedupe_key:
                    existing = connection.execute(
                        """
                        SELECT * FROM notification_outbox
                        WHERE dedupe_key=? AND status='pending'
                        """,
                        (str(dedupe_key),),
                    ).fetchone()
                    if existing:
                        connection.execute("COMMIT")
                        return dict(existing)
                connection.execute(
                    """
                    INSERT INTO notification_outbox(
                        id, session_id, origin, kind, text, status, attempts,
                        last_error, dedupe_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                    """,
                    (
                        item_id,
                        session_id,
                        origin,
                        str(kind or "notice")[:40],
                        text,
                        str(reason or "")[:500],
                        str(dedupe_key or "")[:180],
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    "system",
                    "delivery.queued",
                    item_id,
                    {"kind": kind, "origin": origin, "reason": reason},
                )
                row = connection.execute(
                    "SELECT * FROM notification_outbox WHERE id=?", (item_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(row)

    async def list_deliveries(
        self,
        *,
        session_id: str = "",
        status: str = "pending",
        origin: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_deliveries, session_id, status, origin, limit
        )

    def _list_deliveries(
        self,
        session_id: str,
        status: str,
        origin: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if session_id:
            clauses.append("session_id=?")
            values.append(session_id)
        if status:
            clauses.append("status=?")
            values.append(status)
        if origin:
            clauses.append("origin=?")
            values.append(origin)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(max(1, min(500, int(limit or 100))))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM notification_outbox{where} ORDER BY created_at ASC LIMIT ?",
                tuple(values),
            ).fetchall()
        return [dict(row) for row in rows]

    async def finish_delivery(
        self,
        delivery_id: str,
        *,
        success: bool,
        error: str = "",
        delivered_on_reply: bool = False,
    ) -> dict[str, Any]:
        return await self._run(
            self._finish_delivery,
            delivery_id,
            success,
            error,
            delivered_on_reply,
        )

    def _finish_delivery(
        self,
        delivery_id: str,
        success: bool,
        error: str,
        delivered_on_reply: bool,
    ) -> dict[str, Any]:
        now = utc_now()
        status = "delivered_on_reply" if success and delivered_on_reply else (
            "sent" if success else "pending"
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE notification_outbox
                    SET status=?, attempts=attempts+1, last_error=?,
                        delivered_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        status,
                        "" if success else str(error or "发送失败")[:500],
                        now if success else "",
                        now,
                        delivery_id,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM notification_outbox WHERE id=?", (delivery_id,)
                ).fetchone()
                if not row:
                    raise ValueError("待投递通知不存在")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(row)

    async def dismiss_delivery(self, delivery_id: str, actor_id: str) -> dict[str, Any]:
        return await self._run(self._dismiss_delivery, delivery_id, actor_id)

    def _dismiss_delivery(self, delivery_id: str, actor_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM notification_outbox WHERE id=?", (delivery_id,)
                ).fetchone()
                if not row:
                    raise ValueError("待投递通知不存在")
                connection.execute(
                    "UPDATE notification_outbox SET status='dismissed', updated_at=? WHERE id=?",
                    (now, delivery_id),
                )
                self._insert_audit(
                    connection,
                    str(row["session_id"] or ""),
                    actor_id,
                    "delivery.dismissed",
                    delivery_id,
                    {},
                )
                updated = connection.execute(
                    "SELECT * FROM notification_outbox WHERE id=?", (delivery_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(updated)


__all__ = ["DeliveryRepositoryMixin"]
