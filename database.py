"""
Асинхронный слой БД: SQLite (aiosqlite) локально или PostgreSQL (asyncpg)
при наличии DATABASE_URL=postgres://... / postgresql://...

На Render без Persistent Disk SQLite теряет данные при редеплое —
для продакшена задайте DATABASE_URL (Managed PostgreSQL).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

from models import Category, ChatSession, Order, Product, User
from shop_db import migrate_shop_schema, patch_user_from_row

logger = logging.getLogger(__name__)

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]


def _normalize_database_url(url: str) -> str:
    """Render часто отдаёт postgres:// — asyncpg ждёт postgresql://."""
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


def _qmark_to_dollar(sql: str) -> str:
    """Заменяет ? на $1, $2, ... для asyncpg."""
    parts: list[str] = []
    n = 0
    for ch in sql:
        if ch == "?":
            n += 1
            parts.append(f"${n}")
        else:
            parts.append(ch)
    return "".join(parts)


_ORDER_CLIENT_SELECT = """
            SELECT o.*,
                   u.telegram_id AS client_telegram_id,
                   u.name AS client_name,
                   u.phone AS client_phone,
                   COALESCE(
                       NULLIF(TRIM(o.shipping_address), ''),
                       u.address
                   ) AS client_address
            FROM orders o
            JOIN users u ON u.id = o.user_id
"""


class Database:
    """Единый API для users / orders / chat_sessions (SQLite или PostgreSQL)."""

    def __init__(
        self,
        path: str,
        database_url: str = "",
        debug: bool = False,
    ) -> None:
        self.path = path
        self.database_url = (database_url or "").strip()
        self.debug = debug
        self._use_pg = self.database_url.startswith(("postgres://", "postgresql://"))
        self._conn: aiosqlite.Connection | None = None
        self._pool: Any = None  # asyncpg.Pool

    @property
    def is_postgres(self) -> bool:
        return self._use_pg

    async def connect(self) -> None:
        """Открывает соединение/пул и создаёт таблицы."""
        if self._use_pg:
            if asyncpg is None:
                raise RuntimeError(
                    "DATABASE_URL указывает на PostgreSQL, но пакет asyncpg "
                    "не установлен. Выполните: pip install asyncpg"
                )
            dsn = _normalize_database_url(self.database_url)
            self._pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5)
            await self._create_tables_pg()
            await migrate_shop_schema(self)
            logger.info("PostgreSQL подключён через DATABASE_URL")
            return

        if not self.debug:
            logger.warning(
                "Используется SQLite (%s). Для продакшена на Render задайте "
                "DATABASE_URL (PostgreSQL) или Persistent Disk.",
                self.path,
            )
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout = 8000")
        await self._create_tables_sqlite()
        await migrate_shop_schema(self)
        logger.info("SQLite подключена (WAL): %s", self.path)

    async def close(self) -> None:
        """Закрывает пул или файл БД."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("Пулл PostgreSQL закрыт")
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("Соединение SQLite закрыто")

    # --------------------------------------------------------------- DDL

    async def _create_tables_sqlite(self) -> None:
        assert self._conn is not None
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                name TEXT,
                phone TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                items_json TEXT NOT NULL,
                total INTEGER NOT NULL,
                delivery_cost INTEGER NOT NULL,
                final_total INTEGER NOT NULL,
                status TEXT DEFAULT 'new',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER,
                manager_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                active BOOLEAN DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (order_id) REFERENCES orders(id)
            );

            -- ==== ИЗМЕНЕНИЕ: каталог товаров (Avito sync) ====
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                avito_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                price INTEGER NOT NULL DEFAULT 0,
                stock INTEGER NOT NULL DEFAULT 0,
                category TEXT DEFAULT '',
                image_url TEXT DEFAULT '',
                image_url_2 TEXT DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await self._ensure_product_columns_sqlite()
        await self._ensure_indexes_sqlite()
        await self._conn.commit()

    async def _create_tables_pg(self) -> None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    name TEXT,
                    phone TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    items_json TEXT NOT NULL,
                    total INTEGER NOT NULL,
                    delivery_cost INTEGER NOT NULL,
                    final_total INTEGER NOT NULL,
                    status TEXT DEFAULT 'new',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id SERIAL PRIMARY KEY,
                    order_id INTEGER REFERENCES orders(id),
                    manager_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            # ==== ИЗМЕНЕНИЕ: каталог товаров ====
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS products (
                    id SERIAL PRIMARY KEY,
                    avito_id TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    price INTEGER NOT NULL DEFAULT 0,
                    stock INTEGER NOT NULL DEFAULT 0,
                    category TEXT DEFAULT '',
                    image_url TEXT DEFAULT '',
                    image_url_2 TEXT DEFAULT '',
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            await conn.execute(
                """
                ALTER TABLE products
                ADD COLUMN IF NOT EXISTS image_url_2 TEXT DEFAULT ''
                """
            )
            await self._ensure_indexes_pg(conn)

    async def _ensure_indexes_sqlite(self) -> None:
        assert self._conn is not None
        await self._conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);
            CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
            CREATE INDEX IF NOT EXISTS idx_chat_sessions_active ON chat_sessions(active);
            CREATE INDEX IF NOT EXISTS idx_products_avito_id ON products(avito_id);
            CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
            CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand);
            """
        )

    async def _ensure_indexes_pg(self, conn: Any) -> None:
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_sessions_active ON chat_sessions(active)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_avito_id ON products(avito_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_category ON products(category)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand)"
        )

    async def _ensure_product_columns_sqlite(self) -> None:
        """Добавляет новые колонки products в уже существующую SQLite-БД."""
        assert self._conn is not None
        async with self._conn.execute("PRAGMA table_info(products)") as cursor:
            rows = await cursor.fetchall()
        cols = {str(r[1]) for r in rows}
        if "image_url_2" not in cols:
            await self._conn.execute(
                "ALTER TABLE products ADD COLUMN image_url_2 TEXT DEFAULT ''"
            )
        if "brand" not in cols:
            await self._conn.execute(
                "ALTER TABLE products ADD COLUMN brand TEXT DEFAULT ''"
            )

    # ----------------------------------------------------------- execute

    async def _fetchone(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> Any | None:
        if self._use_pg:
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                return await conn.fetchrow(_qmark_to_dollar(sql), *params)
        assert self._conn is not None
        async with self._conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def _fetchall(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[Any]:
        if self._use_pg:
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(_qmark_to_dollar(sql), *params)
                return list(rows)
        assert self._conn is not None
        async with self._conn.execute(sql, params) as cursor:
            return await cursor.fetchall()

    async def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """UPDATE/DELETE без RETURNING. Возвращает rowcount."""
        if self._use_pg:
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                status = await conn.execute(_qmark_to_dollar(sql), *params)
                # status вида "UPDATE 3"
                try:
                    return int(str(status).split()[-1])
                except (ValueError, IndexError):
                    return 0
        assert self._conn is not None
        cursor = await self._conn.execute(sql, params)
        await self._conn.commit()
        return cursor.rowcount

    async def _insert(self, sql: str, params: tuple[Any, ...]) -> int:
        """INSERT; возвращает id новой строки."""
        if self._use_pg:
            assert self._pool is not None
            # Добавляем RETURNING id, если его ещё нет
            pg_sql = sql.rstrip().rstrip(";")
            if "returning" not in pg_sql.lower():
                pg_sql = f"{pg_sql} RETURNING id"
            async with self._pool.acquire() as conn:
                row_id = await conn.fetchval(_qmark_to_dollar(pg_sql), *params)
                return int(row_id)
        assert self._conn is not None
        cursor = await self._conn.execute(sql, params)
        await self._conn.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("INSERT не вернул lastrowid")
        return int(cursor.lastrowid)

    # ------------------------------------------------------------------ users

    async def get_or_create_user(
        self,
        telegram_id: int,
        name: str | None = None,
        phone: str | None = None,
    ) -> User:
        row = await self._fetchone(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        if row:
            if name or phone:
                await self._execute(
                    """
                    UPDATE users
                    SET name = COALESCE(?, name),
                        phone = COALESCE(?, phone)
                    WHERE telegram_id = ?
                    """,
                    (name, phone, telegram_id),
                )
                row = await self._fetchone(
                    "SELECT * FROM users WHERE telegram_id = ?",
                    (telegram_id,),
                )
            return self._row_to_user(row)

        user_id = await self._insert(
            "INSERT INTO users (telegram_id, name, phone) VALUES (?, ?, ?)",
            (telegram_id, name, phone),
        )
        return User(id=user_id, telegram_id=telegram_id, name=name, phone=phone)

    async def get_user_by_telegram_id(self, telegram_id: int) -> User | None:
        row = await self._fetchone(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        return self._row_to_user(row) if row else None

    async def get_user_by_id(self, user_id: int) -> User | None:
        row = await self._fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
        return self._row_to_user(row) if row else None

    # ----------------------------------------------------------------- orders

    async def create_order(
        self,
        user_id: int,
        items_json: str,
        total: int,
        delivery_cost: int,
        final_total: int,
        status: str = "new",
        points_spent: int = 0,
        points_earned: int = 0,
        cash_paid: int = 0,
        shipping_address: str = "",
        stock_held: int = 0,
    ) -> Order:
        cash = int(cash_paid) if cash_paid else max(0, int(final_total) - int(points_spent or 0))
        order_id = await self._insert(
            """
            INSERT INTO orders
                (user_id, items_json, total, delivery_cost, final_total, status,
                 points_spent, points_earned, cash_paid, shipping_address, stock_held)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                items_json,
                total,
                delivery_cost,
                final_total,
                status,
                int(points_spent or 0),
                int(points_earned or 0),
                cash,
                (shipping_address or "").strip(),
                int(stock_held or 0),
            ),
        )
        order = await self.get_order_by_id(order_id)
        if order is None:
            raise RuntimeError("Не удалось прочитать созданный заказ")
        return order

    async def reserve_and_create_order(self, **kwargs: Any) -> tuple[Order, bool]:
        from order_transactions import reserve_and_create_order as _reserve

        return await _reserve(self, **kwargs)

    async def complete_order_with_loyalty(
        self,
        order_id: int,
        deductions: list[tuple[int, int]] | None = None,
    ) -> tuple[bool, str, int, int]:
        from order_transactions import complete_order_with_loyalty as _complete

        return await _complete(self, order_id, deductions)

    async def cancel_order_restore_stock(self, order_id: int) -> tuple[bool, str]:
        from order_transactions import cancel_order_restore_stock as _cancel

        return await _cancel(self, order_id)

    async def ping(self) -> bool:
        row = await self._fetchone("SELECT 1 AS ok")
        return row is not None

    async def get_order_by_id(self, order_id: int) -> Order | None:
        row = await self._fetchone(
            _ORDER_CLIENT_SELECT + " WHERE o.id = ?",
            (order_id,),
        )
        return self._row_to_order(row) if row else None

    async def get_orders_by_telegram_id(self, telegram_id: int) -> list[Order]:
        rows = await self._fetchall(
            _ORDER_CLIENT_SELECT
            + " WHERE u.telegram_id = ? ORDER BY o.created_at DESC",
            (telegram_id,),
        )
        return [self._row_to_order(r) for r in rows]

    async def get_reserved_quantities(self) -> dict[int, int]:
        """
        Сколько единиц зарезервировано открытыми заказами (new/processing).
        Ключ — product.id. Старые заказы без id в JSON пропускаются.
        """
        rows = await self._fetchall(
            """
            SELECT items_json FROM orders
            WHERE status IN ('new', 'processing')
            """
        )
        reserved: dict[int, int] = {}
        for row in rows:
            try:
                raw = json.loads(row["items_json"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(raw, list):
                continue
            for item in raw:
                if not isinstance(item, dict):
                    continue
                pid_raw = item.get("id", item.get("product_id"))
                try:
                    pid = int(pid_raw)
                    qty = int(item.get("quantity") or 0)
                except (TypeError, ValueError):
                    continue
                if pid > 0 and qty > 0:
                    reserved[pid] = reserved.get(pid, 0) + qty
        return reserved

    async def get_all_orders(self, status: str | None = None) -> list[Order]:
        if status:
            rows = await self._fetchall(
                _ORDER_CLIENT_SELECT
                + " WHERE o.status = ? ORDER BY o.created_at DESC",
                (status,),
            )
        else:
            rows = await self._fetchall(
                _ORDER_CLIENT_SELECT + " ORDER BY o.created_at DESC"
            )
        return [self._row_to_order(r) for r in rows]

    async def update_order_status(self, order_id: int, status: str) -> bool:
        return (await self._execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (status, order_id),
        )) > 0

    # ---------------------------------------------------------- chat_sessions

    async def create_chat_session(
        self,
        manager_id: int,
        user_id: int,
        order_id: int | None = None,
    ) -> ChatSession:
        await self._execute(
            """
            UPDATE chat_sessions
            SET active = ?
            WHERE active = ? AND (user_id = ? OR manager_id = ?)
            """,
            (False if self._use_pg else 0, True if self._use_pg else 1, user_id, manager_id),
        )
        session_id = await self._insert(
            """
            INSERT INTO chat_sessions (order_id, manager_id, user_id, active)
            VALUES (?, ?, ?, ?)
            """,
            (order_id, manager_id, user_id, True if self._use_pg else 1),
        )
        session = await self.get_chat_session_by_id(session_id)
        if session is None:
            raise RuntimeError("Не удалось прочитать созданную сессию чата")
        return session

    async def get_chat_session_by_id(self, session_id: int) -> ChatSession | None:
        row = await self._fetchone(
            "SELECT * FROM chat_sessions WHERE id = ?",
            (session_id,),
        )
        return self._row_to_chat(row) if row else None

    async def get_active_chat_for_user(self, telegram_id: int) -> ChatSession | None:
        active = True if self._use_pg else 1
        row = await self._fetchone(
            """
            SELECT * FROM chat_sessions
            WHERE active = ? AND (user_id = ? OR manager_id = ?)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (active, telegram_id, telegram_id),
        )
        return self._row_to_chat(row) if row else None

    async def get_active_chats(self) -> list[ChatSession]:
        active = True if self._use_pg else 1
        rows = await self._fetchall(
            """
            SELECT * FROM chat_sessions
            WHERE active = ?
            ORDER BY created_at DESC
            """,
            (active,),
        )
        return [self._row_to_chat(r) for r in rows]

    async def close_chat_session(self, session_id: int) -> bool:
        return (await self._execute(
            "UPDATE chat_sessions SET active = ? WHERE id = ?",
            (False if self._use_pg else 0, session_id),
        )) > 0

    async def close_active_chats_for_user(self, telegram_id: int) -> int:
        return await self._execute(
            """
            UPDATE chat_sessions
            SET active = ?
            WHERE active = ? AND (user_id = ? OR manager_id = ?)
            """,
            (
                False if self._use_pg else 0,
                True if self._use_pg else 1,
                telegram_id,
                telegram_id,
            ),
        )

    async def get_managers_with_active_chats(self) -> set[int]:
        active = True if self._use_pg else 1
        rows = await self._fetchall(
            "SELECT DISTINCT manager_id FROM chat_sessions WHERE active = ?",
            (active,),
        )
        return {int(r["manager_id"]) for r in rows}

    # -------------------------------------------------------------- products

    async def count_products(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS cnt FROM products")
        if row is None:
            return 0
        try:
            return int(row["cnt"])
        except (KeyError, TypeError, IndexError):
            return int(row[0])

    async def get_all_products(self) -> list[Product]:
        rows = await self._fetchall(
            "SELECT * FROM products ORDER BY id ASC"
        )
        return [self._row_to_product(r) for r in rows]

    async def get_product_by_id(self, product_id: int) -> Product | None:
        row = await self._fetchone(
            "SELECT * FROM products WHERE id = ?",
            (product_id,),
        )
        return self._row_to_product(row) if row else None

    async def get_product_by_avito_id(self, avito_id: str) -> Product | None:
        row = await self._fetchone(
            "SELECT * FROM products WHERE avito_id = ?",
            (str(avito_id),),
        )
        return self._row_to_product(row) if row else None

    async def upsert_product(
        self,
        *,
        avito_id: str,
        name: str,
        description: str = "",
        price: int = 0,
        stock: int = 0,
        category: str = "",
        image_url: str = "",
        update_stock: bool = False,
    ) -> Product:
        """
        Создаёт или обновляет товар по avito_id.

        stock при UPDATE по умолчанию не трогаем (остатки ведут заказы Mini App).
        Передайте update_stock=True, чтобы принудительно перезаписать остаток.
        """
        existing = await self.get_product_by_avito_id(avito_id)
        if existing:
            new_image = (image_url or "").strip()
            old_image = (existing.image_url or "").strip()
            old_local = old_image.startswith(("/static/", "static/"))
            new_local = new_image.startswith(("/static/", "static/"))
            # Локальные фото (скрин/скачивание) всегда важнее HTTP CDN
            if old_local and not new_local:
                keep_image = old_image
            elif new_local:
                keep_image = new_image
            elif new_image and not old_image:
                keep_image = new_image
            elif old_image:
                keep_image = old_image
            else:
                keep_image = new_image

            # Полные описания со страниц Avito не затираем коротким stub из list API
            old_desc = (existing.description or "").strip()
            new_desc = (description or "").strip()
            keep_desc = new_desc
            if old_desc:
                stub_new = len(new_desc) < 80 and " · " in new_desc
                if not new_desc or stub_new or len(old_desc) > len(new_desc) + 40:
                    keep_desc = old_desc

            if update_stock:
                if self._use_pg:
                    await self._execute(
                        """
                        UPDATE products SET
                            name = ?, description = ?, price = ?, stock = ?,
                            category = ?, image_url = ?, updated_at = NOW()
                        WHERE avito_id = ?
                        """,
                        (
                            name,
                            keep_desc,
                            int(price),
                            int(stock),
                            category,
                            keep_image,
                            str(avito_id),
                        ),
                    )
                else:
                    await self._execute(
                        """
                        UPDATE products SET
                            name = ?, description = ?, price = ?, stock = ?,
                            category = ?, image_url = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE avito_id = ?
                        """,
                        (
                            name,
                            keep_desc,
                            int(price),
                            int(stock),
                            category,
                            keep_image,
                            str(avito_id),
                        ),
                    )
            else:
                # stock намеренно не обновляем — сохраняем локальные продажи
                if self._use_pg:
                    await self._execute(
                        """
                        UPDATE products SET
                            name = ?, description = ?, price = ?,
                            category = ?, image_url = ?, updated_at = NOW()
                        WHERE avito_id = ?
                        """,
                        (
                            name,
                            keep_desc,
                            int(price),
                            category,
                            keep_image,
                            str(avito_id),
                        ),
                    )
                else:
                    await self._execute(
                        """
                        UPDATE products SET
                            name = ?, description = ?, price = ?,
                            category = ?, image_url = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE avito_id = ?
                        """,
                        (
                            name,
                            keep_desc,
                            int(price),
                            category,
                            keep_image,
                            str(avito_id),
                        ),
                    )
            product = await self.get_product_by_avito_id(avito_id)
            assert product is not None
            return product

        await self._insert(
            """
            INSERT INTO products
                (avito_id, name, description, price, stock, category, image_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(avito_id),
                name,
                description,
                int(price),
                int(stock),
                category,
                image_url,
            ),
        )
        product = await self.get_product_by_avito_id(avito_id)
        if product is None:
            raise RuntimeError("Не удалось прочитать созданный товар")
        return product

    async def upsert_products(self, items: list[dict[str, Any]]) -> dict[str, int]:
        """
        Массовый upsert.
        Возвращает {"total", "created", "updated", "stock_preserved"}.
        Остатки (stock) всегда обновляются из Avito Stock API.
        """
        total = created = updated = 0
        for raw in items:
            avito_id = str(raw.get("avito_id") or "").strip()
            name = str(raw.get("name") or "").strip()
            if not avito_id or not name:
                continue
            existed = await self.get_product_by_avito_id(avito_id) is not None
            await self.upsert_product(
                avito_id=avito_id,
                name=name,
                description=str(raw.get("description") or ""),
                price=int(raw.get("price") or 0),
                stock=int(raw.get("stock") or 0),
                category=str(raw.get("category") or ""),
                image_url=str(raw.get("image_url") or ""),
                update_stock=True,
            )
            total += 1
            if existed:
                updated += 1
            else:
                created += 1
        return {
            "total": total,
            "created": created,
            "updated": updated,
            "stock_preserved": 0,
        }

    async def delete_products_not_in_avito_ids(self, keep_ids: list[str]) -> int:
        """
        Удаляет товары, которых нет в keep_ids (после успешного sync Avito).
        Также удаляет demo-* и пустые avito_id.
        """
        keep = {str(x).strip() for x in keep_ids if str(x).strip()}
        if not keep:
            return 0

        rows = await self._fetchall("SELECT id, avito_id FROM products")
        to_delete: list[int] = []
        for row in rows:
            aid = str(row["avito_id"] or "").strip()
            if not aid or aid.startswith("demo-") or aid not in keep:
                to_delete.append(int(row["id"]))

        for pid in to_delete:
            await self._execute("DELETE FROM products WHERE id = ?", (pid,))
        return len(to_delete)

    async def delete_test_products(self) -> int:
        """Удаляет demo / пустой avito_id (команда /clean_test)."""
        rows = await self._fetchall("SELECT id, avito_id FROM products")
        to_delete: list[int] = []
        for row in rows:
            aid = str(row["avito_id"] or "").strip()
            if not aid or aid.startswith("demo-"):
                to_delete.append(int(row["id"]))
        for pid in to_delete:
            await self._execute("DELETE FROM products WHERE id = ?", (pid,))
        return len(to_delete)

    async def decrement_product_stock(self, product_id: int, quantity: int) -> bool:
        """
        Атомарно списывает quantity со склада.
        Успех только если stock >= quantity (защита от гонки).
        """
        if quantity < 1:
            return False
        rows = await self._execute(
            """
            UPDATE products
            SET stock = stock - ?
            WHERE id = ? AND stock >= ?
            """,
            (int(quantity), int(product_id), int(quantity)),
        )
        return rows > 0

    async def increment_product_stock(self, product_id: int, quantity: int) -> bool:
        """Возврат остатка (откат при частичном списании)."""
        if quantity < 1:
            return False
        rows = await self._execute(
            "UPDATE products SET stock = stock + ? WHERE id = ?",
            (int(quantity), int(product_id)),
        )
        return rows > 0

    async def complete_order_with_stock(
        self,
        order_id: int,
        deductions: list[tuple[int, int]],
    ) -> tuple[bool, str, int]:
        """
        Совместимость: закрытие + loyalty. Возвращает (ok, err, deducted_lines).
        Предпочтительно вызывать complete_order_with_loyalty.
        """
        ok, err, deducted, _earned = await self.complete_order_with_loyalty(
            order_id, deductions
        )
        return ok, err, deducted

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _as_str(value: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    @classmethod
    def _row_keys(cls, row: Any) -> set[str]:
        if hasattr(row, "keys"):
            return set(row.keys())
        return set()

    @classmethod
    def _row_to_user(cls, row: Any) -> User:
        keys = cls._row_keys(row)
        user = User(
            id=int(row["id"]),
            telegram_id=int(row["telegram_id"]),
            name=row["name"],
            phone=row["phone"],
            created_at=cls._as_str(row["created_at"]),
        )
        return patch_user_from_row(user, row, keys)

    @classmethod
    def _row_to_order(cls, row: Any) -> Order:
        keys = cls._row_keys(row)
        return Order(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            items_json=row["items_json"],
            total=int(row["total"]),
            delivery_cost=int(row["delivery_cost"]),
            final_total=int(row["final_total"]),
            status=row["status"],
            created_at=cls._as_str(row["created_at"]),
            client_telegram_id=(
                int(row["client_telegram_id"])
                if "client_telegram_id" in keys and row["client_telegram_id"] is not None
                else None
            ),
            client_name=row["client_name"] if "client_name" in keys else None,
            client_phone=row["client_phone"] if "client_phone" in keys else None,
            client_address=(
                str(row["client_address"] or "")
                if "client_address" in keys
                else None
            ),
            shipping_address=(
                str(row["shipping_address"] or "")
                if "shipping_address" in keys
                else None
            ),
            points_spent=int(row["points_spent"] or 0) if "points_spent" in keys else 0,
            points_earned=int(row["points_earned"] or 0) if "points_earned" in keys else 0,
            cash_paid=int(row["cash_paid"] or 0) if "cash_paid" in keys else 0,
            stock_held=int(row["stock_held"] or 0) if "stock_held" in keys else 0,
        )

    @classmethod
    def _row_to_chat(cls, row: Any) -> ChatSession:
        return ChatSession(
            id=int(row["id"]),
            order_id=row["order_id"],
            manager_id=int(row["manager_id"]),
            user_id=int(row["user_id"]),
            active=bool(row["active"]),
            created_at=cls._as_str(row["created_at"]),
        )

    @classmethod
    def _row_to_product(cls, row: Any) -> Product:
        keys = cls._row_keys(row)
        return Product(
            id=int(row["id"]),
            avito_id=str(row["avito_id"]),
            name=str(row["name"]),
            description=str(row["description"] or ""),
            price=int(row["price"] or 0),
            stock=int(row["stock"] or 0),
            category=str(row["category"] or ""),
            brand=str(row["brand"] or "") if "brand" in keys else "",
            image_url=str(row["image_url"] or ""),
            image_url_2=str(row["image_url_2"] or "") if "image_url_2" in keys else "",
            updated_at=cls._as_str(row["updated_at"]),
        )
