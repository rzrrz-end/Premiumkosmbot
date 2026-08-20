"""
Миграции и CRUD для ручного каталога, профиля, избранного и лояльности.
Подключается из Database.connect().
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from models import Brand, Category, Product, User

logger = logging.getLogger(__name__)

DEFAULT_CATEGORIES: list[dict[str, Any]] = [
    {"id": "aromati", "title": "Ароматы", "image_url": "/static/img/aromati.png", "sort_order": 10},
    {"id": "polost", "title": "Уход за полостью рта", "image_url": "/static/img/polost-rta.png", "sort_order": 20},
    {"id": "telom", "title": "Уход за телом", "image_url": "/static/img/uhod-za-telom.png", "sort_order": 30},
    {"id": "stajling", "title": "Стайлинг", "image_url": "/static/img/stajling.png", "sort_order": 40},
    {"id": "licom", "title": "Уход за кожей лица", "image_url": "/static/img/uhod-za-licom.png", "sort_order": 50},
    {"id": "shampun", "title": "Шампунь", "image_url": "/static/img/shampun.png", "sort_order": 60},
    {"id": "kondicioner", "title": "Кондиционер", "image_url": "/static/img/kondicioner.png", "sort_order": 70},
    {"id": "brite", "title": "Бритьё", "image_url": "/static/img/brite.png", "sort_order": 80},
    {"id": "boroda", "title": "Борода", "image_url": "/static/img/boroda.png", "sort_order": 90},
]

# Одноразовая эвристика для старых товаров без category
_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("polost", ("marvis", "зубн", "полост")),
    ("boroda", ("бород", "усов", "усы", "beard")),
    ("brite", ("брит", "aftershave", "помазок", "прешейв")),
    ("shampun", ("шампун", "shampoo")),
    ("kondicioner", ("кондицион", "conditioner")),
    ("aromati", ("одеколон", "cologne", "парфюм", "дезодорант", "аромат")),
    ("licom", ("для лица", "умыван", "сыворот", "face")),
    ("telom", ("гель для душа", "для тела", "для рук", "body")),
    ("stajling", ("уклад", "стайлинг", "pomade", "помад", "clay", "глина")),
]


def _slugify(value: str) -> str:
    raw = (value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9а-яё_-]+", "-", raw, flags=re.I)
    raw = re.sub(r"-{2,}", "-", raw).strip("-")
    return (raw or "cat")[:40]


async def migrate_shop_schema(db: Any) -> None:
    """Добавляет колонки/таблицы магазина (идемпотентно)."""
    if db.is_postgres:
        await _migrate_pg(db)
    else:
        await _migrate_sqlite(db)
    await _seed_categories(db)
    await _backfill_product_categories(db)
    await _seed_brands_from_products(db)


async def _migrate_sqlite(db: Any) -> None:
    assert db._conn is not None
    await db._conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            image_url TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS brands (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            image_url TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, product_id)
        );
        """
    )
    # users
    async with db._conn.execute("PRAGMA table_info(users)") as cur:
        user_cols = {str(r[1]) for r in await cur.fetchall()}
    for col, ddl in (
        ("address", "ALTER TABLE users ADD COLUMN address TEXT DEFAULT ''"),
        ("loyalty_points", "ALTER TABLE users ADD COLUMN loyalty_points INTEGER DEFAULT 0"),
        ("lifetime_spent", "ALTER TABLE users ADD COLUMN lifetime_spent INTEGER DEFAULT 0"),
        ("cart_json", "ALTER TABLE users ADD COLUMN cart_json TEXT DEFAULT '{}'"),
    ):
        if col not in user_cols:
            await db._conn.execute(ddl)

    # orders
    async with db._conn.execute("PRAGMA table_info(orders)") as cur:
        order_cols = {str(r[1]) for r in await cur.fetchall()}
    for col, ddl in (
        ("points_spent", "ALTER TABLE orders ADD COLUMN points_spent INTEGER DEFAULT 0"),
        ("points_earned", "ALTER TABLE orders ADD COLUMN points_earned INTEGER DEFAULT 0"),
        ("cash_paid", "ALTER TABLE orders ADD COLUMN cash_paid INTEGER DEFAULT 0"),
        ("shipping_address", "ALTER TABLE orders ADD COLUMN shipping_address TEXT DEFAULT ''"),
        ("stock_held", "ALTER TABLE orders ADD COLUMN stock_held INTEGER DEFAULT 0"),
    ):
        if col not in order_cols:
            await db._conn.execute(ddl)

    await db._conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            key TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            order_id INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_idempotency_user ON idempotency_keys(user_id);
        CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
        CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
        """
    )

    # products — image_url_2 уже мог быть
    async with db._conn.execute("PRAGMA table_info(products)") as cur:
        prod_cols = {str(r[1]) for r in await cur.fetchall()}
    if "image_url_2" not in prod_cols:
        await db._conn.execute(
            "ALTER TABLE products ADD COLUMN image_url_2 TEXT DEFAULT ''"
        )
    if "brand" not in prod_cols:
        await db._conn.execute(
            "ALTER TABLE products ADD COLUMN brand TEXT DEFAULT ''"
        )
    await db._conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand)"
    )
    await db._conn.commit()
    await _backfill_shipping_address_sqlite(db)
    await _hold_stock_for_legacy_open_orders(db)
    await _backfill_product_brands(db)


async def _migrate_pg(db: Any) -> None:
    assert db._pool is not None
    async with db._pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                image_url TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0,
                active BOOLEAN DEFAULT TRUE
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS brands (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                image_url TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0,
                active BOOLEAN DEFAULT TRUE
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (user_id, product_id)
            )
            """
        )
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS address TEXT DEFAULT ''")
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS loyalty_points INTEGER DEFAULT 0"
        )
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS lifetime_spent INTEGER DEFAULT 0"
        )
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS cart_json TEXT DEFAULT '{}'"
        )
        await conn.execute(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS points_spent INTEGER DEFAULT 0"
        )
        await conn.execute(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS points_earned INTEGER DEFAULT 0"
        )
        await conn.execute(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS cash_paid INTEGER DEFAULT 0"
        )
        await conn.execute(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_address TEXT DEFAULT ''"
        )
        await conn.execute(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS stock_held INTEGER DEFAULT 0"
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                key TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                order_id INTEGER NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_idempotency_user ON idempotency_keys(user_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_category ON products(category)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at)"
        )
        await conn.execute(
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS image_url_2 TEXT DEFAULT ''"
        )
        await conn.execute(
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS brand TEXT DEFAULT ''"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand)"
        )
        await conn.execute(
            """
            UPDATE orders o
            SET shipping_address = COALESCE(NULLIF(TRIM(u.address), ''), o.shipping_address)
            FROM users u
            WHERE o.user_id = u.id
              AND (o.shipping_address IS NULL OR TRIM(o.shipping_address) = '')
            """
        )
    await _hold_stock_for_legacy_open_orders(db)
    await _backfill_product_brands(db)


async def _backfill_shipping_address_sqlite(db: Any) -> None:
    """Старые заказы без snapshot — копируем текущий address пользователя."""
    assert db._conn is not None
    await db._conn.execute(
        """
        UPDATE orders
        SET shipping_address = (
            SELECT TRIM(COALESCE(u.address, ''))
            FROM users u WHERE u.id = orders.user_id
        )
        WHERE shipping_address IS NULL OR TRIM(shipping_address) = ''
        """
    )
    await db._conn.commit()


async def _hold_stock_for_legacy_open_orders(db: Any) -> None:
    """
    Старые new/processing без stock_held: один раз списываем склад,
    чтобы модель совпала с reserve-at-create.
    """
    import json

    rows = await db._fetchall(
        """
        SELECT id, items_json, stock_held FROM orders
        WHERE status IN ('new', 'processing')
          AND COALESCE(stock_held, 0) = 0
        """
    )
    for row in rows:
        order_id = int(row["id"])
        try:
            raw = json.loads(row["items_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        need: dict[int, int] = {}
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                pid = int(item.get("id") or item.get("product_id") or 0)
                qty = int(item.get("quantity") or 0)
            except (TypeError, ValueError):
                continue
            if pid >= 1 and qty >= 1:
                need[pid] = need.get(pid, 0) + qty
        for pid, qty in need.items():
            updated = await db._execute(
                "UPDATE products SET stock = stock - ? "
                "WHERE id = ? AND stock >= ?",
                (qty, pid, qty),
            )
            if updated < 1:
                await db._execute(
                    "UPDATE products SET stock = CASE WHEN stock > ? THEN stock - ? ELSE 0 END "
                    "WHERE id = ?",
                    (qty, qty, pid),
                )
                logger.warning(
                    "Legacy order #%s: неполный hold stock для product %s qty %s",
                    order_id,
                    pid,
                    qty,
                )
        await db._execute(
            "UPDATE orders SET stock_held = 1 WHERE id = ?",
            (order_id,),
        )
        if need:
            logger.info("Legacy order #%s: stock held", order_id)


def guess_brand(name: str) -> str:
    """Вытаскивает бренд из названия (латиница в начале или известный бренд)."""
    raw = (name or "").strip()
    if not raw:
        return ""
    low = f" {raw.lower()} "
    known = (
        "Lock Stock & Barrel",
        "Lock Stock Barrel",
        "The Bluebeards Revenge",
        "18.21 Man Made",
        "American Crew",
        "Captain Fawcett",
        "Dear Beard",
        "Dapper Dan",
        "Hawkins & Brimble",
        "Uppercut Deluxe",
        "Bluebeards Revenge",
        "Clubman Pinaud",
        "The Gentlemans",
        "Lock stock",
        "Reuzel",
        "Marvis",
        "Proraso",
        "Uppercut",
        "Layrite",
        "Suavecito",
        "Murdock",
        "Baxter",
        "Hawkins",
        "Fawcett",
        "Clubman",
        "Gummy",
        "Nivea",
        "Loreal",
        "L'Oreal",
        "Lab Series",
        "Kiehl's",
        "Aesop",
        "Byrd",
        "O'Douds",
        "ODouds",
        "Triumph",
        "Pacific Shaving",
        "Cremo",
        "Bulldog",
        "The Body Shop",
        "Jack Black",
        "Every Man Jack",
        "Harry's",
        "Bevel",
        "Shea Moisture",
        "Duke Cannon",
        "Brickell",
        "Tiege Hanley",
        "Hanz de Fuko",
    )
    # longest first
    for brand in sorted(known, key=len, reverse=True):
        b = brand.strip()
        if not b:
            continue
        bl = b.lower()
        if raw.lower().startswith(bl) or f" {bl} " in low:
            # canonicalize a few aliases
            aliases = {
                "lock stock barrel": "Lock Stock & Barrel",
                "lock stock": "Lock Stock & Barrel",
                "lock stock & barrel": "Lock Stock & Barrel",
                "bluebeards revenge": "The Bluebeards Revenge",
                "the bluebeards revenge": "The Bluebeards Revenge",
                "fawcett": "Captain Fawcett",
                "captain fawcett": "Captain Fawcett",
            }
            return aliases.get(bl, b)
    m = re.match(
        r"^([A-Za-z0-9][A-Za-z0-9&.'’\-]*(?:[ \-][A-Za-z0-9&.'’\-]+){0,3})"
        r"(?=\s+[А-Яа-яЁё]|\s+\d|$)",
        raw,
    )
    if m:
        return m.group(1).strip(" -")
    return ""


async def _backfill_product_brands(db: Any) -> None:
    rows = await db._fetchall("SELECT id, name, brand FROM products")
    for row in rows:
        current = str(row["brand"] or "").strip() if "brand" in row.keys() else ""
        if current:
            continue
        guessed = guess_brand(str(row["name"] or ""))
        if not guessed:
            continue
        await db._execute(
            "UPDATE products SET brand = ? WHERE id = ?",
            (guessed, int(row["id"])),
        )


async def _seed_categories(db: Any) -> None:
    for c in DEFAULT_CATEGORIES:
        existing = await db._fetchone(
            "SELECT id FROM categories WHERE id = ?", (c["id"],)
        )
        if existing:
            continue
        await db._execute(
            """
            INSERT INTO categories (id, title, image_url, sort_order, active)
            VALUES (?, ?, ?, ?, ?)
            """,
            (c["id"], c["title"], c["image_url"], int(c["sort_order"]), 1),
        )


def _guess_category(name: str, description: str) -> str:
    hay = f"{name} {description}".lower()
    for cat_id, words in _CATEGORY_RULES:
        if any(w in hay for w in words):
            return cat_id
    return "stajling"


async def _backfill_product_categories(db: Any) -> None:
    known = {c["id"] for c in DEFAULT_CATEGORIES}
    rows = await db._fetchall("SELECT id, name, description, category FROM products")
    for row in rows:
        cat = str(row["category"] or "").strip()
        if cat in known:
            continue
        guessed = _guess_category(str(row["name"] or ""), str(row["description"] or ""))
        await db._execute(
            "UPDATE products SET category = ? WHERE id = ?",
            (guessed, int(row["id"])),
        )


# ---- helpers attached via Database methods wrapper in database.py ----

async def update_user_profile(
    db: Any,
    telegram_id: int,
    *,
    name: str | None = None,
    phone: str | None = None,
    address: str | None = None,
) -> User:
    user = await db.get_or_create_user(telegram_id=telegram_id, name=name, phone=phone)
    await db._execute(
        """
        UPDATE users SET
            name = COALESCE(?, name),
            phone = COALESCE(?, phone),
            address = COALESCE(?, address)
        WHERE telegram_id = ?
        """,
        (
            name if name is not None else None,
            phone if phone is not None else None,
            address if address is not None else None,
            telegram_id,
        ),
    )
    refreshed = await db.get_user_by_telegram_id(telegram_id)
    assert refreshed is not None
    return refreshed


async def list_categories(db: Any, *, active_only: bool = True) -> list[Category]:
    if active_only:
        rows = await db._fetchall(
            "SELECT * FROM categories WHERE active = 1 OR active = TRUE "
            "ORDER BY sort_order ASC, title ASC"
        )
    else:
        rows = await db._fetchall(
            "SELECT * FROM categories ORDER BY sort_order ASC, title ASC"
        )
    return [_row_to_category(r) for r in rows]


async def upsert_category(
    db: Any,
    *,
    category_id: str,
    title: str,
    image_url: str = "",
    sort_order: int = 0,
    active: bool = True,
) -> Category:
    cid = (category_id or _slugify(title)).strip()
    if not cid:
        cid = f"cat-{uuid.uuid4().hex[:8]}"
    existing = await db._fetchone("SELECT id FROM categories WHERE id = ?", (cid,))
    if existing:
        await db._execute(
            """
            UPDATE categories
            SET title = ?, image_url = ?, sort_order = ?, active = ?
            WHERE id = ?
            """,
            (title, image_url, int(sort_order), 1 if active else 0, cid),
        )
    else:
        await db._execute(
            """
            INSERT INTO categories (id, title, image_url, sort_order, active)
            VALUES (?, ?, ?, ?, ?)
            """,
            (cid, title, image_url, int(sort_order), 1 if active else 0),
        )
    row = await db._fetchone("SELECT * FROM categories WHERE id = ?", (cid,))
    assert row is not None
    return _row_to_category(row)


async def delete_category(db: Any, category_id: str) -> bool:
    rows = await db._execute("DELETE FROM categories WHERE id = ?", (category_id,))
    return rows > 0


def _brand_id_from_name(name: str) -> str:
    raw = (name or "").strip().lower()
    raw = re.sub(r"[^a-z0-9а-яё_-]+", "-", raw, flags=re.I)
    raw = re.sub(r"-{2,}", "-", raw).strip("-")
    return (raw or f"brand-{uuid.uuid4().hex[:8]}")[:60]


def _row_to_brand(row: Any) -> Brand:
    active_raw = row["active"]
    active = (
        bool(active_raw)
        if not isinstance(active_raw, str)
        else active_raw not in ("0", "false", "False")
    )
    return Brand(
        id=str(row["id"]),
        name=str(row["name"] or ""),
        image_url=str(row["image_url"] or ""),
        sort_order=int(row["sort_order"] or 0),
        active=active,
    )


async def list_brands(db: Any, *, active_only: bool = True) -> list[Brand]:
    if active_only:
        rows = await db._fetchall(
            "SELECT * FROM brands WHERE active = 1 OR active = TRUE "
            "ORDER BY sort_order ASC, name ASC"
        )
    else:
        rows = await db._fetchall(
            "SELECT * FROM brands ORDER BY sort_order ASC, name ASC"
        )
    brands = [_row_to_brand(r) for r in rows]
    brands.sort(key=lambda b: (b.sort_order, b.name.lower()))
    return brands


async def get_brand_by_id(db: Any, brand_id: str) -> Brand | None:
    row = await db._fetchone("SELECT * FROM brands WHERE id = ?", (brand_id,))
    return _row_to_brand(row) if row else None


async def get_brand_by_name(db: Any, name: str) -> Brand | None:
    name = (name or "").strip()
    if not name:
        return None
    row = await db._fetchone(
        "SELECT * FROM brands WHERE LOWER(TRIM(name)) = LOWER(?) LIMIT 1",
        (name,),
    )
    return _row_to_brand(row) if row else None


async def upsert_brand(
    db: Any,
    *,
    brand_id: str = "",
    name: str,
    image_url: str | None = None,
    sort_order: int = 0,
    active: bool = True,
    rename_products: bool = True,
) -> Brand:
    name = (name or "").strip()
    if len(name) < 1:
        raise ValueError("empty_brand_name")

    existing: Brand | None = None
    cid = (brand_id or "").strip()
    if cid:
        existing = await get_brand_by_id(db, cid)
    if existing is None:
        by_name = await get_brand_by_name(db, name)
        if by_name is not None:
            existing = by_name
            cid = by_name.id
    if not cid:
        cid = _brand_id_from_name(name)

    if existing:
        old_name = existing.name
        new_image = existing.image_url if image_url is None else image_url
        await db._execute(
            """
            UPDATE brands
            SET name = ?, image_url = ?, sort_order = ?, active = ?
            WHERE id = ?
            """,
            (name, new_image or "", int(sort_order), 1 if active else 0, cid),
        )
        if rename_products and old_name.strip().lower() != name.lower():
            await db._execute(
                "UPDATE products SET brand = ? WHERE LOWER(TRIM(brand)) = LOWER(?)",
                (name, old_name),
            )
    else:
        await db._execute(
            """
            INSERT INTO brands (id, name, image_url, sort_order, active)
            VALUES (?, ?, ?, ?, ?)
            """,
            (cid, name, image_url or "", int(sort_order), 1 if active else 0),
        )

    row = await db._fetchone("SELECT * FROM brands WHERE id = ?", (cid,))
    assert row is not None
    return _row_to_brand(row)


async def delete_brand(db: Any, brand_id: str, *, clear_products: bool = False) -> bool:
    brand = await get_brand_by_id(db, brand_id)
    if brand is None:
        return False
    if clear_products:
        await db._execute(
            "UPDATE products SET brand = '' WHERE LOWER(TRIM(brand)) = LOWER(?)",
            (brand.name,),
        )
    rows = await db._execute("DELETE FROM brands WHERE id = ?", (brand_id,))
    return rows > 0


async def _seed_brands_from_products(db: Any) -> None:
    """Один раз подтягивает уникальные brand из products в таблицу brands."""
    rows = await db._fetchall(
        """
        SELECT DISTINCT TRIM(brand) AS brand
        FROM products
        WHERE brand IS NOT NULL AND TRIM(brand) != ''
        """
    )
    for row in rows:
        name = str(row["brand"] or "").strip()
        if not name:
            continue
        existing = await get_brand_by_name(db, name)
        if existing:
            continue
        try:
            await upsert_brand(db, name=name, active=True)
        except Exception:
            logger.exception("Не удалось создать бренд из products: %s", name)


async def create_product(
    db: Any,
    *,
    name: str,
    description: str = "",
    price: int = 0,
    stock: int = 0,
    category: str = "",
    brand: str = "",
    image_url: str = "",
) -> Product:
    avito_id = f"manual-{uuid.uuid4().hex[:12]}"
    brand_val = (brand or "").strip() or guess_brand(name)
    await db._insert(
        """
        INSERT INTO products
            (avito_id, name, description, price, stock, category, brand, image_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            avito_id,
            name.strip(),
            description or "",
            int(price),
            int(stock),
            category or "",
            brand_val,
            image_url or "",
        ),
    )
    if brand_val:
        try:
            await upsert_brand(db, name=brand_val, active=True)
        except Exception:
            logger.exception("Не удалось upsert бренда при создании товара: %s", brand_val)
    product = await db.get_product_by_avito_id(avito_id)
    assert product is not None
    return product


async def update_product(
    db: Any,
    product_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    price: int | None = None,
    stock: int | None = None,
    category: str | None = None,
    brand: str | None = None,
    image_url: str | None = None,
) -> Product | None:
    product = await db.get_product_by_id(product_id)
    if product is None:
        return None
    new_name = name if name is not None else product.name
    new_brand = brand if brand is not None else product.brand
    if brand is None and not (new_brand or "").strip():
        new_brand = guess_brand(new_name)
    await db._execute(
        """
        UPDATE products SET
            name = ?,
            description = ?,
            price = ?,
            stock = ?,
            category = ?,
            brand = ?,
            image_url = ?
        WHERE id = ?
        """,
        (
            new_name,
            description if description is not None else product.description,
            int(price) if price is not None else int(product.price),
            int(stock) if stock is not None else int(product.stock),
            category if category is not None else product.category,
            (new_brand or "").strip(),
            image_url if image_url is not None else product.image_url,
            int(product_id),
        ),
    )
    cleaned = (new_brand or "").strip()
    if cleaned:
        try:
            await upsert_brand(db, name=cleaned, active=True)
        except Exception:
            logger.exception("Не удалось upsert бренда при обновлении товара: %s", cleaned)
    return await db.get_product_by_id(product_id)


async def delete_product(db: Any, product_id: int) -> bool:
    rows = await db._execute("DELETE FROM products WHERE id = ?", (int(product_id),))
    return rows > 0


async def list_favorite_ids(db: Any, user_id: int) -> list[int]:
    rows = await db._fetchall(
        "SELECT product_id FROM favorites WHERE user_id = ?",
        (int(user_id),),
    )
    return [int(r["product_id"]) for r in rows]


async def set_favorite(db: Any, user_id: int, product_id: int, *, on: bool) -> None:
    if on:
        existing = await db._fetchone(
            "SELECT 1 FROM favorites WHERE user_id = ? AND product_id = ?",
            (int(user_id), int(product_id)),
        )
        if not existing:
            await db._execute(
                "INSERT INTO favorites (user_id, product_id) VALUES (?, ?)",
                (int(user_id), int(product_id)),
            )
    else:
        await db._execute(
            "DELETE FROM favorites WHERE user_id = ? AND product_id = ?",
            (int(user_id), int(product_id)),
        )


def _normalize_cart(raw: Any) -> dict[str, int]:
    if isinstance(raw, str):
        try:
            import json

            raw = json.loads(raw or "{}")
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, val in raw.items():
        try:
            pid = int(key)
            qty = int(val)
        except (TypeError, ValueError):
            continue
        if pid < 1 or qty < 1:
            continue
        out[str(pid)] = qty
    return out


async def get_cart(db: Any, user_id: int) -> dict[str, int]:
    row = await db._fetchone(
        "SELECT cart_json FROM users WHERE id = ?",
        (int(user_id),),
    )
    if not row:
        return {}
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    if "cart_json" not in keys:
        return {}
    return _normalize_cart(row["cart_json"])


async def save_cart(db: Any, user_id: int, cart: Any) -> dict[str, int]:
    import json

    normalized = _normalize_cart(cart)
    await db._execute(
        "UPDATE users SET cart_json = ? WHERE id = ?",
        (json.dumps(normalized, ensure_ascii=False), int(user_id)),
    )
    return normalized


async def spend_loyalty_points(db: Any, user_id: int, points: int) -> bool:
    pts = max(0, int(points))
    if pts <= 0:
        return True
    rows = await db._execute(
        "UPDATE users SET loyalty_points = loyalty_points - ? "
        "WHERE id = ? AND loyalty_points >= ?",
        (pts, int(user_id), pts),
    )
    return rows > 0


async def refund_loyalty_points(db: Any, user_id: int, points: int) -> None:
    pts = max(0, int(points))
    if pts <= 0:
        return
    await db._execute(
        "UPDATE users SET loyalty_points = loyalty_points + ? WHERE id = ?",
        (pts, int(user_id)),
    )


async def complete_order_loyalty(db: Any, order_id: int) -> int:
    """
    Начисляет баллы и lifetime_spent при закрытии заказа.
    Возвращает начисленные баллы.
    """
    from utils.loyalty import calc_points_earn

    order = await db.get_order_by_id(order_id)
    if order is None:
        return 0
    if int(order.points_earned or 0) > 0:
        return int(order.points_earned)
    user = await db.get_user_by_id(order.user_id)
    if user is None:
        return 0
    cash = int(order.cash_paid or 0)
    if cash <= 0:
        cash = max(0, int(order.final_total) - int(order.points_spent or 0))
    earned = calc_points_earn(
        cash_paid=cash,
        lifetime_spent_before=int(user.lifetime_spent or 0),
    )
    await db._execute(
        """
        UPDATE users SET
            loyalty_points = loyalty_points + ?,
            lifetime_spent = lifetime_spent + ?
        WHERE id = ?
        """,
        (earned, cash, int(user.id)),
    )
    await db._execute(
        "UPDATE orders SET points_earned = ?, cash_paid = ? WHERE id = ?",
        (earned, cash, int(order_id)),
    )
    return earned


def _row_to_category(row: Any) -> Category:
    active_raw = row["active"]
    active = bool(active_raw) if not isinstance(active_raw, str) else active_raw not in ("0", "false", "False")
    return Category(
        id=str(row["id"]),
        title=str(row["title"] or ""),
        image_url=str(row["image_url"] or ""),
        sort_order=int(row["sort_order"] or 0),
        active=active,
    )


def patch_user_from_row(user: User, row: Any, keys: set[str]) -> User:
    if "address" in keys:
        user.address = row["address"] or ""
    if "loyalty_points" in keys:
        user.loyalty_points = int(row["loyalty_points"] or 0)
    if "lifetime_spent" in keys:
        user.lifetime_spent = int(row["lifetime_spent"] or 0)
    return user
