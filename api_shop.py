"""
HTTP API мини-приложения: профиль, избранное, категории, админка товаров.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import web
from aiogram import Bot

from config import get_config
from database import Database
from handlers.order import (
    notify_customer_about_payment,
    notify_managers_about_order,
)
from models import OrderItem
from order_transactions import OrderTxError
from shop_db import (
    create_product,
    delete_brand,
    delete_category,
    delete_product,
    get_cart,
    list_brands,
    list_categories,
    list_favorite_ids,
    save_cart,
    set_favorite,
    update_product,
    update_user_profile,
    upsert_brand,
    upsert_category,
)
from utils.loyalty import calc_points_earn, loyalty_rate_percent
from utils.telegram_auth import InitDataError, validate_webapp_init_data

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
PRODUCTS_IMG_DIR = ROOT / "static" / "img" / "products"
BRANDS_IMG_DIR = ROOT / "static" / "img" / "brands"
_BRANDS_CACHE_TTL_SEC = 300.0
_BRANDS_CACHE: dict[str, Any] = {"expires_at": 0.0, "items": []}


def _invalidate_brands_cache() -> None:
    _BRANDS_CACHE["items"] = []
    _BRANDS_CACHE["expires_at"] = 0.0


def _brand_stem(value: str) -> str:
    # Приводим бренд к безопасному имени файла: "Lock Stock & Barrel" -> "lock_stock_barrel"
    cleaned = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ]+", "_", (value or "").strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned.lower()


def _brand_photo_path(brand: str) -> str | None:
    brand = (brand or "").strip()
    if not brand:
        return None
    candidates: list[str] = []
    encoded = quote(brand, safe="")
    stem = _brand_stem(brand)
    for base in (brand, encoded, stem):
        if not base:
            continue
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            candidates.append(base + ext)
    seen: set[str] = set()
    for filename in candidates:
        if filename in seen:
            continue
        seen.add(filename)
        full = BRANDS_IMG_DIR / filename
        if full.exists():
            return f"/static/img/brands/{filename}"
    return None


def _resolve_telegram_id(
    payload: dict[str, Any],
    cfg,
    *,
    require_init_data: bool = False,
    max_age_sec: int | None = None,
) -> int:
    """
    Берёт telegram_id ТОЛЬКО из проверенного initData.
    Клиентские поля user.is_manager / user.telegram_id игнорируются
    (кроме DEBUG-fallback без initData, и то только если require_init_data=False).
    """
    # Никогда не доверяем флагам с клиента
    if isinstance(payload.get("user"), dict):
        payload = {**payload, "user": {k: v for k, v in payload["user"].items() if k != "is_manager"}}

    init_data = payload.get("initData") or payload.get("init_data") or ""
    if isinstance(init_data, str) and init_data.strip():
        kwargs = {}
        if max_age_sec is not None:
            kwargs["max_age_sec"] = max_age_sec
        validated = validate_webapp_init_data(init_data, cfg.bot_token, **kwargs)
        return int(validated.user.id)

    if require_init_data:
        raise InitDataError("Требуется initData от Telegram WebApp")

    if cfg.debug:
        user_raw = payload.get("user") or {}
        tid = user_raw.get("telegram_id")
        if isinstance(tid, int) and tid > 0:
            return tid
        return -1
    raise InitDataError("Требуется initData от Telegram WebApp")


def _is_manager(telegram_id: int, cfg) -> bool:
    """Менеджер = telegram_id из MANAGER_IDS (env). Клиент это не задаёт."""
    try:
        tid = int(telegram_id)
    except (TypeError, ValueError):
        return False
    if tid <= 0:
        return False
    allowed = {int(x) for x in (cfg.manager_ids or []) if int(x) > 0}
    return tid in allowed


async def _require_user(request: web.Request) -> tuple[Database, int, Any]:
    cfg = get_config()
    try:
        payload = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(text="invalid_json") from exc
    try:
        tid = _resolve_telegram_id(payload, cfg, require_init_data=not cfg.debug)
    except InitDataError as exc:
        raise web.HTTPUnauthorized(text=str(exc)) from exc
    db: Database = request.app["db"]
    return db, tid, payload


async def _require_manager(request: web.Request) -> tuple[Database, int, Any]:
    """
    Строгая проверка менеджера для админ-API:
    - обязательный валидный initData (HMAC Telegram), даже в DEBUG;
    - короткий TTL подписи (15 мин);
    - telegram_id только из initData;
    - membership только из MANAGER_IDS на сервере.
    """
    cfg = get_config()
    try:
        payload = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(text="invalid_json") from exc

    try:
        tid = _resolve_telegram_id(
            payload,
            cfg,
            require_init_data=True,
            max_age_sec=900,
        )
    except InitDataError as exc:
        logger.warning("Admin auth rejected (initData): %s", exc)
        raise web.HTTPUnauthorized(text="invalid_init_data") from exc

    if not _is_manager(tid, cfg):
        logger.warning("Admin auth rejected: telegram_id=%s not in MANAGER_IDS", tid)
        raise web.HTTPForbidden(text="managers_only")

    if not _rate_limit(f"admin:{tid}", limit=60, window=60.0):
        raise web.HTTPTooManyRequests(text="rate_limited")

    db: Database = request.app["db"]
    return db, tid, payload


async def _require_manager_multipart(
    *,
    init_data: str,
    cfg,
) -> int:
    """Та же строгая проверка для multipart upload."""
    try:
        tid = _resolve_telegram_id(
            {"initData": init_data},
            cfg,
            require_init_data=True,
            max_age_sec=900,
        )
    except InitDataError as exc:
        logger.warning("Admin upload auth rejected (initData): %s", exc)
        raise web.HTTPUnauthorized(text="invalid_init_data") from exc
    if not _is_manager(tid, cfg):
        logger.warning("Admin upload auth rejected: telegram_id=%s", tid)
        raise web.HTTPForbidden(text="managers_only")
    if not _rate_limit(f"admin:{tid}", limit=60, window=60.0):
        raise web.HTTPTooManyRequests(text="rate_limited")
    return tid


async def handle_get_me(request: web.Request) -> web.Response:
    cfg = get_config()
    db: Database = request.app["db"]
    # GET with initData query is awkward — accept POST body or header
    if request.method == "GET":
        init_data = request.rel_url.query.get("initData") or ""
        payload = {"initData": init_data}
    else:
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "invalid_json"}, status=400)
    try:
        tid = _resolve_telegram_id(payload, cfg)
    except InitDataError as exc:
        return web.json_response(
            {"success": False, "error": "invalid_init_data", "detail": str(exc)},
            status=401,
        )
    name = None
    phone = None
    if isinstance(payload.get("user"), dict):
        name = str(payload["user"].get("name") or "").strip() or None
        phone = str(payload["user"].get("phone") or "").strip() or None
    user = await db.get_or_create_user(telegram_id=tid, name=name, phone=phone)
    # перечитать после миграций полей
    user = await db.get_user_by_telegram_id(tid) or user
    favs = await list_favorite_ids(db, user.id)
    cart = await get_cart(db, user.id)
    return web.json_response(
        {
            "success": True,
            "user": user.to_api_dict(is_manager=_is_manager(tid, cfg)),
            "favorite_ids": favs,
            "cart": cart,
        }
    )


async def handle_update_profile(request: web.Request) -> web.Response:
    try:
        db, tid, payload = await _require_user(request)
    except web.HTTPException as exc:
        return web.json_response({"success": False, "error": exc.text}, status=exc.status)
    name = str(payload.get("name") or "").strip()
    phone = str(payload.get("phone") or "").strip()
    address = str(payload.get("address") or "").strip()
    if name and len(name) < 2:
        return web.json_response({"success": False, "error": "invalid_name"}, status=400)
    if phone and len("".join(ch for ch in phone if ch.isdigit())) < 10:
        return web.json_response({"success": False, "error": "invalid_phone"}, status=400)
    user = await update_user_profile(
        db,
        tid,
        name=name or None,
        phone=phone or None,
        address=address if "address" in payload else None,
    )
    cfg = get_config()
    return web.json_response(
        {"success": True, "user": user.to_api_dict(is_manager=_is_manager(tid, cfg))}
    )


async def handle_get_orders(request: web.Request) -> web.Response:
    try:
        db, tid, _payload = await _require_user(request)
    except web.HTTPException as exc:
        return web.json_response({"success": False, "error": exc.text}, status=exc.status)
    orders = await db.get_orders_by_telegram_id(tid)
    return web.json_response(
        {"success": True, "orders": [o.to_api_dict() for o in orders]}
    )


async def handle_favorites(request: web.Request) -> web.Response:
    try:
        db, tid, payload = await _require_user(request)
    except web.HTTPException as exc:
        return web.json_response({"success": False, "error": exc.text}, status=exc.status)
    user = await db.get_or_create_user(telegram_id=tid)
    if request.method == "GET" or payload.get("action") == "list":
        ids = await list_favorite_ids(db, user.id)
        return web.json_response({"success": True, "favorite_ids": ids})

    product_id = payload.get("product_id")
    try:
        product_id = int(product_id)
    except (TypeError, ValueError):
        return web.json_response({"success": False, "error": "invalid_product"}, status=400)
    on = bool(payload.get("on", True))
    if payload.get("action") == "remove":
        on = False
    await set_favorite(db, user.id, product_id, on=on)
    ids = await list_favorite_ids(db, user.id)
    return web.json_response({"success": True, "favorite_ids": ids})


async def handle_cart(request: web.Request) -> web.Response:
    try:
        db, tid, payload = await _require_user(request)
    except web.HTTPException as exc:
        return web.json_response({"success": False, "error": exc.text}, status=exc.status)
    user = await db.get_or_create_user(telegram_id=tid)
    if payload.get("action") == "get" or "cart" not in payload:
        cart = await get_cart(db, user.id)
        return web.json_response({"success": True, "cart": cart})
    cart = await save_cart(db, user.id, payload.get("cart"))
    return web.json_response({"success": True, "cart": cart})


async def handle_get_categories(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    cats = await list_categories(db, active_only=True)
    return web.json_response(
        {"success": True, "categories": [c.to_api_dict() for c in cats]}
    )


async def handle_get_brands(request: web.Request) -> web.Response:
    now = time.monotonic()
    if _BRANDS_CACHE["expires_at"] > now:
        return web.json_response({"success": True, "brands": _BRANDS_CACHE["items"]})

    try:
        db: Database = request.app["db"]
        brands_rows = await list_brands(db, active_only=True)
        brands: list[dict[str, Any]] = []
        for b in brands_rows:
            photo = (b.image_url or "").strip() or _brand_photo_path(b.name)
            if photo and not photo.startswith(("http://", "https://", "/")):
                photo = "/" + photo.lstrip("/")
            brands.append(
                {
                    "id": b.id,
                    "name": b.name,
                    "photo": photo or None,
                }
            )

        # Фоллбек: бренды только из products, если таблица ещё пуста
        if not brands:
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
                brands.append({"name": name, "photo": _brand_photo_path(name)})
            brands.sort(key=lambda x: str(x["name"]).lower())

        _BRANDS_CACHE["items"] = brands
        _BRANDS_CACHE["expires_at"] = now + _BRANDS_CACHE_TTL_SEC
        return web.json_response({"success": True, "brands": brands})
    except Exception:
        logger.exception("Ошибка GET /api/brands")
        return web.json_response(
            {"success": False, "error": "internal_error"},
            status=500,
        )


async def handle_admin_brands(request: web.Request) -> web.Response:
    """CRUD брендов: list / upsert / delete. Только менеджеры."""
    try:
        db, tid, payload = await _require_manager(request)
    except web.HTTPException as exc:
        return web.json_response({"success": False, "error": exc.text}, status=exc.status)

    action = str(payload.get("action") or "list").strip()
    if action == "list":
        brands = await list_brands(db, active_only=False)
        items = []
        for b in brands:
            d = b.to_api_dict()
            if not d.get("photo"):
                d["photo"] = _brand_photo_path(b.name)
            items.append(d)
        return web.json_response({"success": True, "brands": items})

    if action == "upsert":
        name = str(payload.get("name") or "").strip()
        if len(name) < 2:
            return web.json_response({"success": False, "error": "invalid_name"}, status=400)
        image_url = None
        if "image_url" in payload:
            image_url = str(payload.get("image_url") or "").strip()
        try:
            brand = await upsert_brand(
                db,
                brand_id=str(payload.get("id") or "").strip(),
                name=name,
                image_url=image_url,
                sort_order=int(payload.get("sort_order") or 0),
                active=bool(payload.get("active", True)),
                rename_products=True,
            )
        except ValueError:
            return web.json_response({"success": False, "error": "invalid_name"}, status=400)
        _invalidate_brands_cache()
        logger.info("Admin brand upsert by %s: %s", tid, brand.name)
        d = brand.to_api_dict()
        if not d.get("photo"):
            d["photo"] = _brand_photo_path(brand.name)
        return web.json_response({"success": True, "brand": d})

    if action == "delete":
        bid = str(payload.get("id") or "").strip()
        if not bid:
            return web.json_response({"success": False, "error": "invalid_id"}, status=400)
        ok = await delete_brand(
            db,
            bid,
            clear_products=bool(payload.get("clear_products", False)),
        )
        if ok:
            _invalidate_brands_cache()
            logger.info("Admin brand delete by %s: %s", tid, bid)
        return web.json_response({"success": ok})

    return web.json_response({"success": False, "error": "unknown_action"}, status=400)


async def handle_admin_categories(request: web.Request) -> web.Response:
    try:
        db, _tid, payload = await _require_manager(request)
    except web.HTTPException as exc:
        return web.json_response({"success": False, "error": exc.text}, status=exc.status)

    action = str(payload.get("action") or "list")
    if action == "list":
        cats = await list_categories(db, active_only=False)
        return web.json_response(
            {"success": True, "categories": [c.to_api_dict() for c in cats]}
        )
    if action == "upsert":
        title = str(payload.get("title") or "").strip()
        if len(title) < 2:
            return web.json_response({"success": False, "error": "invalid_title"}, status=400)
        cat = await upsert_category(
            db,
            category_id=str(payload.get("id") or "").strip(),
            title=title,
            image_url=str(payload.get("image_url") or ""),
            sort_order=int(payload.get("sort_order") or 0),
            active=bool(payload.get("active", True)),
        )
        return web.json_response({"success": True, "category": cat.to_api_dict()})
    if action == "delete":
        cid = str(payload.get("id") or "").strip()
        ok = await delete_category(db, cid)
        return web.json_response({"success": ok})
    return web.json_response({"success": False, "error": "unknown_action"}, status=400)


async def handle_admin_product(request: web.Request) -> web.Response:
    """JSON CRUD для товаров (фото — отдельным upload)."""
    try:
        db, _tid, payload = await _require_manager(request)
    except web.HTTPException as exc:
        return web.json_response({"success": False, "error": exc.text}, status=exc.status)

    action = str(payload.get("action") or "").strip()
    if action == "create":
        name = str(payload.get("name") or "").strip()
        if len(name) < 2:
            return web.json_response({"success": False, "error": "invalid_name"}, status=400)
        product = await create_product(
            db,
            name=name,
            description=str(payload.get("description") or ""),
            price=int(payload.get("price") or 0),
            stock=int(payload.get("stock") or 0),
            category=str(payload.get("category") or ""),
            brand=str(payload.get("brand") or ""),
            image_url=str(payload.get("image_url") or ""),
        )
        _invalidate_brands_cache()
        return web.json_response({"success": True, "product": product.to_api_dict()})

    if action == "update":
        try:
            pid = int(payload.get("id"))
        except (TypeError, ValueError):
            return web.json_response({"success": False, "error": "invalid_id"}, status=400)
        fields = {}
        for key in ("name", "description", "category", "brand", "image_url"):
            if key in payload:
                fields[key] = str(payload.get(key) or "")
        for key in ("price", "stock"):
            if key in payload:
                try:
                    fields[key] = int(payload.get(key))
                except (TypeError, ValueError):
                    return web.json_response(
                        {"success": False, "error": f"invalid_{key}"}, status=400
                    )
        product = await update_product(db, pid, **fields)
        if product is None:
            return web.json_response({"success": False, "error": "not_found"}, status=404)
        _invalidate_brands_cache()
        return web.json_response({"success": True, "product": product.to_api_dict()})

    if action == "delete":
        try:
            pid = int(payload.get("id"))
        except (TypeError, ValueError):
            return web.json_response({"success": False, "error": "invalid_id"}, status=400)
        ok = await delete_product(db, pid)
        if ok:
            _invalidate_brands_cache()
        return web.json_response({"success": ok})

    return web.json_response({"success": False, "error": "unknown_action"}, status=400)


async def handle_admin_upload_image(request: web.Request) -> web.Response:
    """
    multipart: initData, file,
    либо product_id (товар), либо brand_id / brand_name (бренд).
    Только менеджеры с валидным initData.
    """
    cfg = get_config()
    db: Database = request.app["db"]
    reader = await request.multipart()
    init_data = ""
    product_id: int | None = None
    brand_id = ""
    brand_name = ""
    file_bytes: bytes | None = None
    filename = "upload.jpg"

    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "initData" or part.name == "init_data":
            init_data = (await part.text()).strip()
        elif part.name == "product_id":
            raw = (await part.text()).strip()
            if raw.isdigit():
                product_id = int(raw)
        elif part.name == "brand_id":
            brand_id = (await part.text()).strip()
        elif part.name == "brand_name":
            brand_name = (await part.text()).strip()
        elif part.name in ("file", "image", "photo"):
            filename = part.filename or filename
            file_bytes = await part.read(decode=False)

    try:
        await _require_manager_multipart(init_data=init_data, cfg=cfg)
    except web.HTTPException as exc:
        return web.json_response({"success": False, "error": exc.text}, status=exc.status)

    if not file_bytes or len(file_bytes) < 100:
        return web.json_response({"success": False, "error": "empty_file"}, status=400)
    if len(file_bytes) > 5 * 1024 * 1024:
        return web.json_response(
            {"success": False, "error": "file_too_large", "detail": "Максимум 5 МБ"},
            status=400,
        )

    ext = ".jpg"
    low = filename.lower()
    is_png = file_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    is_jpeg = file_bytes[:3] == b"\xff\xd8\xff"
    is_webp = file_bytes[:4] == b"RIFF" and file_bytes[8:12] == b"WEBP"
    if is_png or low.endswith(".png"):
        if not is_png:
            return web.json_response(
                {"success": False, "error": "invalid_image"}, status=400
            )
        ext = ".png"
    elif is_webp or low.endswith(".webp"):
        if not is_webp:
            return web.json_response(
                {"success": False, "error": "invalid_image"}, status=400
            )
        ext = ".webp"
    elif is_jpeg or low.endswith((".jpg", ".jpeg")):
        if not is_jpeg:
            return web.json_response(
                {"success": False, "error": "invalid_image"}, status=400
            )
        ext = ".jpg"
    else:
        return web.json_response(
            {"success": False, "error": "unsupported_image_type"}, status=400
        )

    # --- загрузка фото бренда ---
    if brand_id or brand_name:
        from shop_db import get_brand_by_id, get_brand_by_name

        brand = None
        if brand_id:
            brand = await get_brand_by_id(db, brand_id)
        if brand is None and brand_name:
            brand = await get_brand_by_name(db, brand_name)
        if brand is None and brand_name:
            brand = await upsert_brand(db, name=brand_name, active=True)
        if brand is None:
            return web.json_response({"success": False, "error": "brand_not_found"}, status=404)

        BRANDS_IMG_DIR.mkdir(parents=True, exist_ok=True)
        stem = _brand_stem(brand.name) or brand.id
        dest = BRANDS_IMG_DIR / f"{stem}{ext}"
        dest.write_bytes(file_bytes)
        rel = f"/static/img/brands/{dest.name}"
        brand = await upsert_brand(
            db,
            brand_id=brand.id,
            name=brand.name,
            image_url=rel,
            sort_order=brand.sort_order,
            active=brand.active,
            rename_products=False,
        )
        _invalidate_brands_cache()
        return web.json_response(
            {"success": True, "image_url": rel, "brand": brand.to_api_dict()}
        )

    # --- загрузка фото товара ---
    PRODUCTS_IMG_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"manual-{uuid.uuid4().hex[:12]}"
    if product_id:
        stem = f"p{product_id}-{uuid.uuid4().hex[:6]}"
    dest = PRODUCTS_IMG_DIR / f"{stem}{ext}"
    dest.write_bytes(file_bytes)
    rel = f"/static/img/products/{dest.name}"

    product_payload = None
    if product_id:
        product = await update_product(db, product_id, image_url=rel)
        if product:
            product_payload = product.to_api_dict()

    return web.json_response(
        {"success": True, "image_url": rel, "product": product_payload}
    )


async def create_order_with_loyalty(
    *,
    db: Database,
    bot: Bot,
    payload: dict[str, Any],
) -> web.Response:
    """Общая логика заказа с баллами (вызывается из main)."""
    cfg = get_config()
    try:
        telegram_id = _resolve_telegram_id(payload, cfg)
    except InitDataError as exc:
        return web.json_response(
            {"success": False, "error": "invalid_init_data", "detail": str(exc)},
            status=401,
        )

    if not _rate_limit(f"order:{telegram_id}", limit=12, window=60.0):
        return web.json_response(
            {"success": False, "error": "rate_limited"},
            status=429,
        )

    items_raw = payload.get("items")
    user_raw = payload.get("user", {})
    if not isinstance(items_raw, list) or not items_raw:
        return web.json_response({"success": False, "error": "items_required"}, status=400)
    if not isinstance(user_raw, dict):
        return web.json_response({"success": False, "error": "user_required"}, status=400)

    customer_name = str(user_raw.get("name", "")).strip()
    phone = str(user_raw.get("phone", "")).strip()
    address = str(user_raw.get("address", "")).strip()
    if len(customer_name) < 2:
        return web.json_response({"success": False, "error": "invalid_name"}, status=400)
    if len("".join(ch for ch in phone if ch.isdigit())) < 10:
        return web.json_response({"success": False, "error": "invalid_phone"}, status=400)
    if len(address) < 5:
        return web.json_response(
            {
                "success": False,
                "error": "address_required",
                "detail": "Укажите адрес доставки",
            },
            status=400,
        )

    try:
        points_to_spend = int(payload.get("points_to_spend") or 0)
    except (TypeError, ValueError):
        points_to_spend = 0
    if points_to_spend < 0:
        points_to_spend = 0

    idempotency_key = str(payload.get("idempotency_key") or "").strip()[:128] or None

    # Цены только с сервера; остаток проверяется атомарно в reserve_and_create_order
    validated_items: list[dict[str, Any]] = []
    for item in items_raw:
        if not isinstance(item, dict):
            return web.json_response({"success": False, "error": "invalid_item"}, status=400)
        try:
            product_id = int(item.get("id"))
            quantity = int(item.get("quantity", 1))
        except (TypeError, ValueError):
            return web.json_response({"success": False, "error": "invalid_item"}, status=400)
        if product_id < 1 or quantity < 1:
            return web.json_response({"success": False, "error": "invalid_item"}, status=400)

        product = await db.get_product_by_id(product_id)
        if product is None:
            return web.json_response(
                {"success": False, "error": "product_not_found"}, status=400
            )
        if product.price <= 0:
            return web.json_response(
                {"success": False, "error": "invalid_product_price"}, status=400
            )
        validated_items.append(
            {
                "id": product.id,
                "name": product.name,
                "price": int(product.price),
                "quantity": quantity,
            }
        )

    user = await update_user_profile(
        db,
        telegram_id,
        name=customer_name,
        phone=phone,
        address=address,
    )

    order_items = [OrderItem.from_dict(i) for i in validated_items]
    total = sum(i.line_total for i in order_items)
    from models import calc_delivery

    delivery = calc_delivery(total)
    gross = total + delivery
    max_points = min(int(user.loyalty_points or 0), gross)
    if points_to_spend > max_points:
        points_to_spend = max_points
    cash_paid = max(0, gross - points_to_spend)

    try:
        order, created = await db.reserve_and_create_order(
            user_id=user.id,
            items=order_items,
            total=total,
            delivery_cost=delivery,
            final_total=gross,
            cash_paid=cash_paid,
            points_spent=points_to_spend,
            shipping_address=address,
            idempotency_key=idempotency_key,
        )
    except OrderTxError as exc:
        status = 400
        if exc.code == "idempotency_conflict":
            status = 409
        return web.json_response(
            {
                "success": False,
                "error": exc.code,
                "detail": exc.detail or None,
            },
            status=status,
        )

    preview_earn = calc_points_earn(
        cash_paid=int(order.cash_paid or cash_paid),
        lifetime_spent_before=int(user.lifetime_spent or 0),
    )

    if created:
        await notify_managers_about_order(
            bot=bot,
            order=order,
            customer_name=customer_name,
            phone=phone,
            address=address,
        )
        await notify_customer_about_payment(
            bot=bot,
            telegram_id=telegram_id,
            order=order,
        )
        try:
            await save_cart(db, user.id, {})
        except Exception:
            logger.exception(
                "Не удалось очистить корзину после заказа user_id=%s", user.id
            )

    return web.json_response(
        {
            "success": True,
            "order_id": order.id,
            "order_number": order.order_number,
            "total": int(order.total),
            "delivery_cost": int(order.delivery_cost),
            "final_total": int(order.final_total),
            "points_spent": int(order.points_spent or 0),
            "cash_paid": int(order.cash_paid or cash_paid),
            "points_to_earn": preview_earn,
            "loyalty_rate": loyalty_rate_percent(user.lifetime_spent),
            "payment_details": cfg.payment_details,
            "shipping_address": address,
            "idempotent_replay": not created,
        }
    )


def _format_photon_address(props: dict[str, Any]) -> str:
    street = str(props.get("street") or "").strip()
    house = str(props.get("housenumber") or "").strip()
    name = str(props.get("name") or "").strip()
    city = str(
        props.get("city")
        or props.get("town")
        or props.get("village")
        or props.get("municipality")
        or ""
    ).strip()
    state = str(props.get("state") or "").strip()
    district = str(props.get("district") or props.get("county") or "").strip()

    line1_parts: list[str] = []
    if street:
        line1_parts.append(street)
        if house:
            line1_parts.append(house)
    elif name:
        line1_parts.append(name)
        if house and house not in name:
            line1_parts.append(house)

    tail: list[str] = []
    for part in (city, district, state):
        if part and part not in line1_parts and part not in tail:
            tail.append(part)

    chunks = []
    if line1_parts:
        chunks.append(", ".join(line1_parts))
    chunks.extend(tail)
    return ", ".join(chunks).strip(" ,")


_ADDRESS_SUGGEST_HITS: dict[str, list[float]] = {}
_ADDRESS_SUGGEST_LIMIT = 30  # запросов
_ADDRESS_SUGGEST_WINDOW = 60.0  # секунд

_RATE_BUCKETS: dict[str, list[float]] = {}


def _rate_limit(key: str, *, limit: int, window: float) -> bool:
    import time

    now = time.monotonic()
    cutoff = now - window
    fresh = [t for t in _RATE_BUCKETS.get(key, []) if t >= cutoff]
    if len(fresh) >= limit:
        _RATE_BUCKETS[key] = fresh
        return False
    fresh.append(now)
    _RATE_BUCKETS[key] = fresh
    return True


def _address_suggest_allowed(client_ip: str) -> bool:
    return _rate_limit(
        f"addr:{client_ip}",
        limit=_ADDRESS_SUGGEST_LIMIT,
        window=_ADDRESS_SUGGEST_WINDOW,
    )


async def handle_address_suggest(request: web.Request) -> web.Response:
    """Подсказки адресов (Photon / Nominatim), без ключей API."""
    import aiohttp

    peer = request.remote or "unknown"
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        peer = forwarded.split(",")[0].strip() or peer
    if not _address_suggest_allowed(peer):
        return web.json_response(
            {"success": False, "error": "rate_limited", "detail": "Слишком много запросов"},
            status=429,
        )

    q = str(request.query.get("q") or "").strip()
    if len(q) < 3:
        return web.json_response({"success": True, "suggestions": []})

    suggestions: list[str] = []
    timeout = aiohttp.ClientTimeout(total=4)
    headers = {"User-Agent": "cosmetics-bot-miniapp/1.0"}

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            params = {
                "q": q,
                "lang": "ru",
                "limit": "6",
                "lat": "55.75",
                "lon": "37.62",
            }
            async with session.get("https://photon.komoot.io/api/", params=params) as resp:
                if resp.status == 200:
                    payload = await resp.json(content_type=None)
                    for feat in (payload.get("features") or [])[:6]:
                        props = feat.get("properties") or {}
                        country = str(props.get("country") or "").lower()
                        country_code = str(props.get("countrycode") or "").lower()
                        if country_code and country_code not in ("ru", "by", "kz"):
                            continue
                        if not country_code and country and not any(
                            x in country for x in ("рос", "белар", "казах")
                        ):
                            continue
                        text = _format_photon_address(props)
                        if text and text not in suggestions:
                            suggestions.append(text)
    except Exception:
        logger.exception("Photon address suggest failed")

    if not suggestions:
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                params = {
                    "q": q,
                    "format": "json",
                    "addressdetails": "1",
                    "limit": "6",
                    "countrycodes": "ru",
                    "accept-language": "ru",
                }
                async with session.get(
                    "https://nominatim.openstreetmap.org/search", params=params
                ) as resp:
                    if resp.status == 200:
                        rows = await resp.json(content_type=None)
                        for row in rows[:6]:
                            text = str(row.get("display_name") or "").strip()
                            if text and text not in suggestions:
                                suggestions.append(text)
        except Exception:
            logger.exception("Nominatim address suggest failed")

    return web.json_response({"success": True, "suggestions": suggestions[:6]})


def setup_shop_routes(app: web.Application) -> None:
    app.router.add_route("POST", "/api/me", handle_get_me)
    app.router.add_route("GET", "/api/me", handle_get_me)
    app.router.add_post("/api/profile", handle_update_profile)
    app.router.add_post("/api/orders", handle_get_orders)
    app.router.add_post("/api/favorites", handle_favorites)
    app.router.add_post("/api/cart", handle_cart)
    app.router.add_get("/api/categories", handle_get_categories)
    app.router.add_get("/api/brands", handle_get_brands)
    app.router.add_get("/api/address-suggest", handle_address_suggest)
    app.router.add_post("/api/admin/categories", handle_admin_categories)
    app.router.add_post("/api/admin/products", handle_admin_product)
    app.router.add_post("/api/admin/brands", handle_admin_brands)
    app.router.add_post("/api/admin/upload", handle_admin_upload_image)
