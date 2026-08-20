"""
Атомарные операции заказов: резерв+создание, закрытие+лояльность, отмена.

SQLite: отдельное соединение + BEGIN IMMEDIATE (WAL).
PostgreSQL: pool connection + transaction + FOR UPDATE.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from models import Order, OrderItem, items_to_json
from utils.loyalty import calc_points_earn

logger = logging.getLogger(__name__)


class OrderTxError(Exception):
    """Бизнес-ошибка транзакции заказа."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


def _qmark_to_dollar(sql: str) -> str:
    parts: list[str] = []
    n = 0
    for ch in sql:
        if ch == "?":
            n += 1
            parts.append(f"${n}")
        else:
            parts.append(ch)
    return "".join(parts)


async def _open_sqlite(path: str) -> Any:
    import aiosqlite

    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout = 8000")
    return conn


async def reserve_and_create_order(
    db: Any,
    *,
    user_id: int,
    items: list[OrderItem],
    total: int,
    delivery_cost: int,
    final_total: int,
    cash_paid: int,
    points_spent: int,
    shipping_address: str,
    idempotency_key: str | None = None,
) -> tuple[Order, bool]:
    """
    В одной транзакции: идемпотентность → списание баллов → проверка/списание
    stock → INSERT order (shipping_address, stock_held=1).

    Returns: (order, created) — created=False при повторном idempotency_key.
    """
    if not items:
        raise OrderTxError("items_required")
    address = (shipping_address or "").strip()
    if len(address) < 5:
        raise OrderTxError("address_required")

    key = (idempotency_key or "").strip()[:128] or None
    pts = max(0, int(points_spent or 0))
    cash = max(0, int(cash_paid if cash_paid is not None else final_total - pts))
    items_json = items_to_json(items)

    lines: list[tuple[int, int, str, int]] = []
    for it in items:
        if not it.product_id or it.product_id < 1 or it.quantity < 1:
            raise OrderTxError("invalid_item")
        lines.append((int(it.product_id), int(it.quantity), it.name, int(it.price)))
    lines.sort(key=lambda x: x[0])

    if db.is_postgres:
        order_id, created = await _reserve_pg(
            db,
            user_id=user_id,
            lines=lines,
            items_json=items_json,
            total=total,
            delivery_cost=delivery_cost,
            final_total=final_total,
            cash_paid=cash,
            points_spent=pts,
            shipping_address=address,
            idempotency_key=key,
        )
    else:
        order_id, created = await _reserve_sqlite(
            db,
            user_id=user_id,
            lines=lines,
            items_json=items_json,
            total=total,
            delivery_cost=delivery_cost,
            final_total=final_total,
            cash_paid=cash,
            points_spent=pts,
            shipping_address=address,
            idempotency_key=key,
        )

    order = await db.get_order_by_id(order_id)
    if order is None:
        raise RuntimeError("order created but not readable")
    return order, created


async def _idempotent_order_id(conn: Any, *, key: str, user_id: int, pg: bool) -> int | None:
    if pg:
        row = await conn.fetchrow(
            "SELECT order_id, user_id FROM idempotency_keys WHERE key = $1",
            key,
        )
    else:
        async with conn.execute(
            "SELECT order_id, user_id FROM idempotency_keys WHERE key = ?",
            (key,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    if int(row["user_id"]) != int(user_id):
        raise OrderTxError("idempotency_conflict")
    return int(row["order_id"])


async def _reserve_sqlite(
    db: Any,
    *,
    user_id: int,
    lines: list[tuple[int, int, str, int]],
    items_json: str,
    total: int,
    delivery_cost: int,
    final_total: int,
    cash_paid: int,
    points_spent: int,
    shipping_address: str,
    idempotency_key: str | None,
) -> tuple[int, bool]:
    conn = await _open_sqlite(db.path)
    try:
        await conn.execute("BEGIN IMMEDIATE")
        if idempotency_key:
            existing = await _idempotent_order_id(
                conn, key=idempotency_key, user_id=user_id, pg=False
            )
            if existing is not None:
                await conn.commit()
                return existing, False

        if points_spent > 0:
            cur = await conn.execute(
                "UPDATE users SET loyalty_points = loyalty_points - ? "
                "WHERE id = ? AND loyalty_points >= ?",
                (points_spent, user_id, points_spent),
            )
            if cur.rowcount < 1:
                await conn.rollback()
                raise OrderTxError("insufficient_points")

        # Агрегируем qty по product_id (несколько строк одного товара)
        need: dict[int, int] = {}
        for pid, qty, _n, _p in lines:
            need[pid] = need.get(pid, 0) + qty

        for pid in sorted(need.keys()):
            qty = need[pid]
            async with conn.execute(
                "SELECT id, name, stock, price FROM products WHERE id = ?",
                (pid,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                await conn.rollback()
                raise OrderTxError("product_not_found", str(pid))
            stock = int(row["stock"] or 0)
            if stock < qty:
                await conn.rollback()
                raise OrderTxError(
                    "insufficient_stock",
                    f"Недостаточно «{row['name']}». Доступно: {stock}",
                )
            cur = await conn.execute(
                "UPDATE products SET stock = stock - ? WHERE id = ? AND stock >= ?",
                (qty, pid, qty),
            )
            if cur.rowcount < 1:
                await conn.rollback()
                raise OrderTxError("insufficient_stock", str(pid))

        cur = await conn.execute(
            """
            INSERT INTO orders
                (user_id, items_json, total, delivery_cost, final_total, status,
                 points_spent, points_earned, cash_paid, shipping_address, stock_held)
            VALUES (?, ?, ?, ?, ?, 'new', ?, 0, ?, ?, 1)
            """,
            (
                user_id,
                items_json,
                total,
                delivery_cost,
                final_total,
                points_spent,
                cash_paid,
                shipping_address,
            ),
        )
        order_id = int(cur.lastrowid)
        if idempotency_key:
            await conn.execute(
                "INSERT INTO idempotency_keys (key, user_id, order_id) VALUES (?, ?, ?)",
                (idempotency_key, user_id, order_id),
            )
        await conn.commit()
        return order_id, True
    except OrderTxError:
        try:
            await conn.rollback()
        except Exception:
            pass
        raise
    except Exception:
        try:
            await conn.rollback()
        except Exception:
            pass
        raise
    finally:
        await conn.close()


async def _reserve_pg(
    db: Any,
    *,
    user_id: int,
    lines: list[tuple[int, int, str, int]],
    items_json: str,
    total: int,
    delivery_cost: int,
    final_total: int,
    cash_paid: int,
    points_spent: int,
    shipping_address: str,
    idempotency_key: str | None,
) -> tuple[int, bool]:
    assert db._pool is not None

    async with db._pool.acquire() as conn:
        async with conn.transaction():
            if idempotency_key:
                existing = await _idempotent_order_id(
                    conn, key=idempotency_key, user_id=user_id, pg=True
                )
                if existing is not None:
                    return existing, False

            if points_spent > 0:
                status = await conn.execute(
                    "UPDATE users SET loyalty_points = loyalty_points - $1 "
                    "WHERE id = $2 AND loyalty_points >= $1",
                    points_spent,
                    user_id,
                )
                if not str(status).endswith(" 1"):
                    raise OrderTxError("insufficient_points")

            need: dict[int, int] = {}
            for pid, qty, _n, _p in lines:
                need[pid] = need.get(pid, 0) + qty

            for pid in sorted(need.keys()):
                qty = need[pid]
                row = await conn.fetchrow(
                    "SELECT id, name, stock, price FROM products WHERE id = $1 FOR UPDATE",
                    pid,
                )
                if row is None:
                    raise OrderTxError("product_not_found", str(pid))
                stock = int(row["stock"] or 0)
                if stock < qty:
                    raise OrderTxError(
                        "insufficient_stock",
                        f"Недостаточно «{row['name']}». Доступно: {stock}",
                    )
                status = await conn.execute(
                    "UPDATE products SET stock = stock - $1 WHERE id = $2 AND stock >= $1",
                    qty,
                    pid,
                )
                if not str(status).endswith(" 1"):
                    raise OrderTxError("insufficient_stock", str(pid))

            order_id = await conn.fetchval(
                """
                INSERT INTO orders
                    (user_id, items_json, total, delivery_cost, final_total, status,
                     points_spent, points_earned, cash_paid, shipping_address, stock_held)
                VALUES ($1, $2, $3, $4, $5, 'new', $6, 0, $7, $8, 1)
                RETURNING id
                """,
                user_id,
                items_json,
                total,
                delivery_cost,
                final_total,
                points_spent,
                cash_paid,
                shipping_address,
            )
            if idempotency_key:
                await conn.execute(
                    "INSERT INTO idempotency_keys (key, user_id, order_id) "
                    "VALUES ($1, $2, $3)",
                    idempotency_key,
                    user_id,
                    int(order_id),
                )
            return int(order_id), True


async def complete_order_with_loyalty(
    db: Any,
    order_id: int,
    deductions: list[tuple[int, int]] | None = None,
) -> tuple[bool, str, int, int]:
    """
    Закрытие заказа + начисление баллов в одной TX.

    Если stock_held=1 — склад уже списан при создании.
    Если stock_held=0 (старые заказы) — списываем deductions сейчас.

    Returns: (ok, error_code, deducted_lines, points_earned)
    """
    order_id = int(order_id)
    cleaned: list[tuple[int, int]] = []
    for pid, qty in deductions or []:
        try:
            pid_i, qty_i = int(pid), int(qty)
        except (TypeError, ValueError):
            continue
        if pid_i >= 1 and qty_i >= 1:
            cleaned.append((pid_i, qty_i))
    cleaned.sort(key=lambda x: x[0])

    if db.is_postgres:
        return await _complete_pg(db, order_id, cleaned)
    return await _complete_sqlite(db, order_id, cleaned)


async def _complete_sqlite(
    db: Any, order_id: int, deductions: list[tuple[int, int]]
) -> tuple[bool, str, int, int]:
    conn = await _open_sqlite(db.path)
    try:
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute(
            "SELECT * FROM orders WHERE id = ?",
            (order_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            await conn.rollback()
            return False, "order_not_found", 0, 0
        status = str(row["status"] or "")
        if status == "completed":
            earned = int(row["points_earned"] or 0) if "points_earned" in row.keys() else 0
            await conn.rollback()
            return False, "already_completed", 0, earned
        if status == "cancelled":
            await conn.rollback()
            return False, "cancelled", 0, 0

        keys = set(row.keys())
        stock_held = int(row["stock_held"] or 0) if "stock_held" in keys else 0
        deducted = 0

        if stock_held == 0:
            # Legacy: списание при закрытии
            if not deductions:
                # из items_json
                try:
                    raw = json.loads(row["items_json"] or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    raw = []
                for item in raw if isinstance(raw, list) else []:
                    if not isinstance(item, dict):
                        continue
                    try:
                        pid = int(item.get("id") or item.get("product_id") or 0)
                        qty = int(item.get("quantity") or 0)
                    except (TypeError, ValueError):
                        continue
                    if pid >= 1 and qty >= 1:
                        deductions.append((pid, qty))
                deductions.sort(key=lambda x: x[0])
            for pid, qty in deductions:
                cur = await conn.execute(
                    "UPDATE products SET stock = stock - ? "
                    "WHERE id = ? AND stock >= ?",
                    (qty, pid, qty),
                )
                if cur.rowcount < 1:
                    await conn.rollback()
                    return False, f"insufficient_stock:{pid}", deducted, 0
                deducted += 1
            await conn.execute(
                "UPDATE orders SET stock_held = 1 WHERE id = ?",
                (order_id,),
            )

        # Loyalty (идемпотентно: только если points_earned == 0)
        already_earned = int(row["points_earned"] or 0) if "points_earned" in keys else 0
        cash = int(row["cash_paid"] or 0) if "cash_paid" in keys else 0
        if cash <= 0:
            cash = max(
                0,
                int(row["final_total"] or 0)
                - int(row["points_spent"] or 0 if "points_spent" in keys else 0),
            )
        user_id = int(row["user_id"])
        earned = already_earned
        if already_earned == 0:
            async with conn.execute(
                "SELECT lifetime_spent FROM users WHERE id = ?",
                (user_id,),
            ) as cur:
                urow = await cur.fetchone()
            lifetime = int(urow["lifetime_spent"] or 0) if urow else 0
            earned = calc_points_earn(cash_paid=cash, lifetime_spent_before=lifetime)
            await conn.execute(
                """
                UPDATE users SET
                    loyalty_points = loyalty_points + ?,
                    lifetime_spent = lifetime_spent + ?
                WHERE id = ?
                """,
                (earned, cash, user_id),
            )
            await conn.execute(
                "UPDATE orders SET points_earned = ?, cash_paid = ? WHERE id = ?",
                (earned, cash, order_id),
            )

        await conn.execute(
            "UPDATE orders SET status = 'completed' WHERE id = ? AND status != 'completed'",
            (order_id,),
        )
        await conn.commit()
        return True, "", deducted, int(earned)
    except Exception:
        try:
            await conn.rollback()
        except Exception:
            pass
        raise
    finally:
        await conn.close()


async def _complete_pg(
    db: Any, order_id: int, deductions: list[tuple[int, int]]
) -> tuple[bool, str, int, int]:
    assert db._pool is not None

    class _Abort(Exception):
        def __init__(self, code: str, earned: int = 0) -> None:
            self.code = code
            self.earned = earned

    try:
        async with db._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM orders WHERE id = $1 FOR UPDATE",
                    order_id,
                )
                if row is None:
                    raise _Abort("order_not_found")
                status = str(row["status"] or "")
                if status == "completed":
                    raise _Abort("already_completed", int(row["points_earned"] or 0))
                if status == "cancelled":
                    raise _Abort("cancelled")

                stock_held = int(row["stock_held"] or 0)
                deducted = 0
                ded = list(deductions)
                if stock_held == 0:
                    if not ded:
                        try:
                            raw = json.loads(row["items_json"] or "[]")
                        except (TypeError, ValueError, json.JSONDecodeError):
                            raw = []
                        for item in raw if isinstance(raw, list) else []:
                            if not isinstance(item, dict):
                                continue
                            try:
                                pid = int(item.get("id") or item.get("product_id") or 0)
                                qty = int(item.get("quantity") or 0)
                            except (TypeError, ValueError):
                                continue
                            if pid >= 1 and qty >= 1:
                                ded.append((pid, qty))
                        ded.sort(key=lambda x: x[0])
                    for pid, qty in ded:
                        result = await conn.execute(
                            "UPDATE products SET stock = stock - $1 "
                            "WHERE id = $2 AND stock >= $1",
                            qty,
                            pid,
                        )
                        if not str(result).endswith(" 1"):
                            raise _Abort(f"insufficient_stock:{pid}")
                        deducted += 1
                    await conn.execute(
                        "UPDATE orders SET stock_held = 1 WHERE id = $1",
                        order_id,
                    )

                already_earned = int(row["points_earned"] or 0)
                cash = int(row["cash_paid"] or 0)
                if cash <= 0:
                    cash = max(
                        0,
                        int(row["final_total"] or 0) - int(row["points_spent"] or 0),
                    )
                user_id = int(row["user_id"])
                earned = already_earned
                if already_earned == 0:
                    urow = await conn.fetchrow(
                        "SELECT lifetime_spent FROM users WHERE id = $1 FOR UPDATE",
                        user_id,
                    )
                    lifetime = int(urow["lifetime_spent"] or 0) if urow else 0
                    earned = calc_points_earn(
                        cash_paid=cash, lifetime_spent_before=lifetime
                    )
                    await conn.execute(
                        """
                        UPDATE users SET
                            loyalty_points = loyalty_points + $1,
                            lifetime_spent = lifetime_spent + $2
                        WHERE id = $3
                        """,
                        earned,
                        cash,
                        user_id,
                    )
                    await conn.execute(
                        "UPDATE orders SET points_earned = $1, cash_paid = $2 WHERE id = $3",
                        earned,
                        cash,
                        order_id,
                    )

                await conn.execute(
                    "UPDATE orders SET status = 'completed' WHERE id = $1",
                    order_id,
                )
                return True, "", deducted, int(earned)
    except _Abort as exc:
        return False, exc.code, 0, exc.earned


async def cancel_order_restore_stock(
    db: Any,
    order_id: int,
) -> tuple[bool, str]:
    """
    Отмена new/processing: вернуть stock (если stock_held), вернуть баллы, status=cancelled.
    """
    order_id = int(order_id)
    if db.is_postgres:
        return await _cancel_pg(db, order_id)
    return await _cancel_sqlite(db, order_id)


async def _cancel_sqlite(db: Any, order_id: int) -> tuple[bool, str]:
    conn = await _open_sqlite(db.path)
    try:
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute(
            "SELECT * FROM orders WHERE id = ?",
            (order_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            await conn.rollback()
            return False, "order_not_found"
        status = str(row["status"] or "")
        if status == "completed":
            await conn.rollback()
            return False, "already_completed"
        if status == "cancelled":
            await conn.commit()
            return True, "already_cancelled"

        keys = set(row.keys())
        stock_held = int(row["stock_held"] or 0) if "stock_held" in keys else 0
        if stock_held == 1:
            try:
                raw = json.loads(row["items_json"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = []
            restore: dict[int, int] = {}
            for item in raw if isinstance(raw, list) else []:
                if not isinstance(item, dict):
                    continue
                try:
                    pid = int(item.get("id") or item.get("product_id") or 0)
                    qty = int(item.get("quantity") or 0)
                except (TypeError, ValueError):
                    continue
                if pid >= 1 and qty >= 1:
                    restore[pid] = restore.get(pid, 0) + qty
            for pid in sorted(restore.keys()):
                await conn.execute(
                    "UPDATE products SET stock = stock + ? WHERE id = ?",
                    (restore[pid], pid),
                )
            await conn.execute(
                "UPDATE orders SET stock_held = 0 WHERE id = ?",
                (order_id,),
            )

        pts = int(row["points_spent"] or 0) if "points_spent" in keys else 0
        if pts > 0:
            await conn.execute(
                "UPDATE users SET loyalty_points = loyalty_points + ? WHERE id = ?",
                (pts, int(row["user_id"])),
            )

        await conn.execute(
            "UPDATE orders SET status = 'cancelled' WHERE id = ?",
            (order_id,),
        )
        await conn.commit()
        return True, ""
    except Exception:
        try:
            await conn.rollback()
        except Exception:
            pass
        raise
    finally:
        await conn.close()


async def _cancel_pg(db: Any, order_id: int) -> tuple[bool, str]:
    assert db._pool is not None

    class _Abort(Exception):
        def __init__(self, code: str) -> None:
            self.code = code

    try:
        async with db._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM orders WHERE id = $1 FOR UPDATE",
                    order_id,
                )
                if row is None:
                    raise _Abort("order_not_found")
                status = str(row["status"] or "")
                if status == "completed":
                    raise _Abort("already_completed")
                if status == "cancelled":
                    return True, "already_cancelled"

                stock_held = int(row["stock_held"] or 0)
                if stock_held == 1:
                    try:
                        raw = json.loads(row["items_json"] or "[]")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        raw = []
                    restore: dict[int, int] = {}
                    for item in raw if isinstance(raw, list) else []:
                        if not isinstance(item, dict):
                            continue
                        try:
                            pid = int(item.get("id") or item.get("product_id") or 0)
                            qty = int(item.get("quantity") or 0)
                        except (TypeError, ValueError):
                            continue
                        if pid >= 1 and qty >= 1:
                            restore[pid] = restore.get(pid, 0) + qty
                    for pid in sorted(restore.keys()):
                        await conn.execute(
                            "UPDATE products SET stock = stock + $1 WHERE id = $2",
                            restore[pid],
                            pid,
                        )
                    await conn.execute(
                        "UPDATE orders SET stock_held = 0 WHERE id = $1",
                        order_id,
                    )

                pts = int(row["points_spent"] or 0)
                if pts > 0:
                    await conn.execute(
                        "UPDATE users SET loyalty_points = loyalty_points + $1 WHERE id = $2",
                        pts,
                        int(row["user_id"]),
                    )
                await conn.execute(
                    "UPDATE orders SET status = 'cancelled' WHERE id = $1",
                    order_id,
                )
                return True, ""
    except _Abort as exc:
        return False, exc.code
