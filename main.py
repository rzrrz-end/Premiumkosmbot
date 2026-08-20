"""
Точка входа Telegram-бота + веб-сервер (aiohttp) в одном процессе.
Готово к деплою на Render (PORT, healthcheck, CORS, initData).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any

from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import TelegramObject

from api_shop import create_order_with_loyalty, setup_shop_routes
from config import get_config
from database import Database
from handlers import register_routers
from handlers.chat import force_close_chat
from handlers.start import start_contact_with_manager
from utils.telegram_auth import InitDataError, validate_webapp_init_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# Общий стоп-флаг для graceful shutdown
_stop_event: asyncio.Event | None = None


class DatabaseMiddleware(BaseMiddleware):
    """Прокидывает экземпляр Database в хендлеры."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(self, handler, event: TelegramObject, data: dict):
        data["db"] = self.db
        return await handler(event, data)


def _mask_proxy_url(proxy_url: str) -> str:
    """Скрывает пароль в URL прокси для логов."""
    try:
        if "@" not in proxy_url or "://" not in proxy_url:
            return proxy_url
        scheme, rest = proxy_url.split("://", 1)
        creds, host = rest.rsplit("@", 1)
        if ":" in creds:
            user, _password = creds.split(":", 1)
            return f"{scheme}://{user}:***@{host}"
        return f"{scheme}://***@{host}"
    except Exception:
        return "***"


def _cors_headers(request: web.Request | None = None) -> dict[str, str]:
    """CORS: только разрешённые origin из CORS_ORIGIN / MINI_APP_URL."""
    from urllib.parse import urlparse

    def _as_origin(value: str) -> str:
        raw = (value or "").strip()
        if not raw:
            return ""
        if "://" not in raw:
            raw = "https://" + raw
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.netloc:
            return value.rstrip("/")
        return f"{parsed.scheme}://{parsed.netloc}"

    cfg = get_config()
    origin = ""
    if request is not None:
        origin = request.headers.get("Origin", "").strip()

    allowed = {_as_origin(o) for o in cfg.cors_origins if o}
    origin_n = _as_origin(origin) if origin else ""

    if cfg.debug and not allowed:
        allow_origin = origin or "*"
    elif origin_n and origin_n in allowed:
        allow_origin = origin  # echo точный Origin из запроса
    elif allowed:
        # Origin не совпал — не открываем доступ
        allow_origin = "null"
    else:
        allow_origin = "null"

    return {
        "Access-Control-Allow-Origin": allow_origin,
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Telegram-Init-Data",
        "Access-Control-Max-Age": "86400",
        "Vary": "Origin",
    }


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=_cors_headers(request))
    response = await handler(request)
    for key, value in _cors_headers(request).items():
        response.headers[key] = value
    return response


# ==== ИЗМЕНЕНИЕ: товары из БД (после sync Avito), а не заглушка ====
async def _get_products_payload(db: Database) -> list[dict[str, Any]]:
    """
    Каталог для Mini App.
    stock в ответе = доступно к продаже (склад Avito минус открытые заказы).
    """
    products = await db.get_all_products()
    reserved = await db.get_reserved_quantities()
    payload: list[dict[str, Any]] = []
    for p in products:
        row = p.to_api_dict()
        warehouse = int(p.stock or 0)
        avail = max(warehouse - int(reserved.get(p.id, 0)), 0)
        row["stock"] = avail
        row["stock_warehouse"] = warehouse
        payload.append(row)
    return payload


def _resolve_telegram_id(payload: dict[str, Any], cfg) -> int:
    """
    Берёт telegram_id только из проверенного initData.
    В DEBUG допускается fallback (локальный тест без Telegram).
    """
    init_data = (
        payload.get("initData")
        or payload.get("init_data")
        or ""
    )
    if isinstance(init_data, str) and init_data.strip():
        validated = validate_webapp_init_data(init_data, cfg.bot_token)
        return validated.user.id

    if cfg.debug:
        user_raw = payload.get("user") or {}
        tid = user_raw.get("telegram_id")
        if isinstance(tid, int) and tid > 0:
            logger.warning("DEBUG: telegram_id взят из клиента без initData")
            return tid
        return -1

    raise InitDataError("Требуется initData от Telegram WebApp")


def _client_name_from_payload(payload: dict[str, Any], cfg) -> str:
    """Имя клиента из initData или из тела запроса."""
    init_data = payload.get("initData") or payload.get("init_data") or ""
    if isinstance(init_data, str) and init_data.strip():
        try:
            validated = validate_webapp_init_data(init_data, cfg.bot_token)
            u = validated.user
            parts = [u.first_name or "", u.last_name or ""]
            name = " ".join(p for p in parts if p).strip()
            if name:
                return name
            if u.username:
                return u.username
        except InitDataError:
            pass
    user_raw = payload.get("user") or {}
    if isinstance(user_raw, dict):
        name = str(user_raw.get("name") or "").strip()
        if name:
            return name
    return "Клиент"


async def _handle_health(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    try:
        ok = await db.ping()
    except Exception:
        logger.exception("health db ping failed")
        return web.json_response(
            {"status": "error", "db": False},
            status=503,
        )
    if not ok:
        return web.json_response(
            {"status": "error", "db": False},
            status=503,
        )
    return web.json_response({"status": "ok", "db": True})


async def _handle_get_products(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    try:
        products = await _get_products_payload(db)
        return web.json_response({"success": True, "products": products})
    except Exception:
        logger.exception("Ошибка GET /api/products")
        return web.json_response(
            {"success": False, "error": "internal_error"},
            status=500,
        )


async def _handle_create_order(request: web.Request) -> web.Response:
    """POST /api/order — заказ с проверкой остатков и списанием баллов."""
    db: Database = request.app["db"]
    bot: Bot = request.app["bot"]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response(
            {"success": False, "error": "invalid_json"},
            status=400,
        )
    try:
        return await create_order_with_loyalty(db=db, bot=bot, payload=payload)
    except Exception:
        logger.exception("Ошибка POST /api/order")
        return web.json_response(
            {"success": False, "error": "internal_error"},
            status=500,
        )


async def _handle_contact_manager(request: web.Request) -> web.Response:
    """
    POST /api/contact-manager — старт чата с менеджером из Mini App.
    Требует initData (как /api/order).
    """
    db: Database = request.app["db"]
    bot: Bot = request.app["bot"]
    config = get_config()
    try:
        payload = await request.json()
    except Exception:
        return web.json_response(
            {"success": False, "error": "invalid_json"},
            status=400,
        )

    try:
        try:
            telegram_id = _resolve_telegram_id(payload, config)
        except InitDataError as exc:
            logger.warning("initData отклонён (contact-manager): %s", exc)
            return web.json_response(
                {"success": False, "error": "invalid_init_data", "detail": str(exc)},
                status=401,
            )
        if telegram_id < 1:
            return web.json_response(
                {"success": False, "error": "invalid_user"},
                status=401,
            )

        client_name = _client_name_from_payload(payload, config)

        await force_close_chat(
            bot,
            db,
            telegram_id,
            notify_user=True,
            notify_peer=True,
        )

        notices: list[str] = []

        async def answer(text: str) -> None:
            notices.append(text)
            try:
                await bot.send_message(telegram_id, text)
            except Exception:
                logger.exception(
                    "Не удалось написать клиенту %s после contact-manager",
                    telegram_id,
                )

        await start_contact_with_manager(
            bot=bot,
            db=db,
            client_id=telegram_id,
            client_name=client_name,
            answer=answer,
        )

        return web.json_response(
            {
                "success": True,
                "message": notices[0] if notices else "Менеджер подключён",
            }
        )
    except Exception:
        logger.exception("Ошибка POST /api/contact-manager")
        return web.json_response(
            {"success": False, "error": "internal_error"},
            status=500,
        )


async def _handle_mini_app(_request: web.Request) -> web.FileResponse:
    from pathlib import Path

    # Telegram WebView агрессивно кэширует HTML — запрещаем кэш для mini-app.
    resp = web.FileResponse(Path(__file__).resolve().parent / "mini-app.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def create_web_app(
    db: Database,
    bot: Bot,
    dp: Dispatcher | None = None,
) -> web.Application:
    from pathlib import Path

    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    cfg = get_config()
    static_dir = Path(__file__).resolve().parent / "static"
    app = web.Application(middlewares=[cors_middleware], client_max_size=25 * 1024 * 1024)
    app["db"] = db
    app["bot"] = bot
    app.router.add_get("/health", _handle_health)
    app.router.add_get("/", _handle_mini_app)
    app.router.add_get("/mini-app.html", _handle_mini_app)
    app.router.add_get("/api/products", _handle_get_products)
    app.router.add_post("/api/order", _handle_create_order)
    app.router.add_post("/api/contact-manager", _handle_contact_manager)
    setup_shop_routes(app)
    app.router.add_static("/static/", path=str(static_dir), name="static")

    if cfg.use_webhook and dp is not None:
        secret = cfg.webhook_secret or None
        SimpleRequestHandler(
            dispatcher=dp,
            bot=bot,
            secret_token=secret,
        ).register(app, path=cfg.webhook_path)
        setup_application(app, dp, bot=bot)
        logger.info("Webhook handler: %s", cfg.webhook_path)

    return app


async def run_web_server(
    db: Database,
    bot: Bot,
    stop_event: asyncio.Event,
    dp: Dispatcher | None = None,
) -> None:
    cfg = get_config()
    app = create_web_app(db, bot, dp=dp)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=cfg.port)
    await site.start()
    logger.info("HTTP API: http://0.0.0.0:%s (health: /health)", cfg.port)

    if cfg.use_webhook:
        url = cfg.webhook_url
        await bot.set_webhook(
            url=url,
            secret_token=cfg.webhook_secret or None,
            drop_pending_updates=False,
            allowed_updates=dp.resolve_used_update_types() if dp else None,
            ip_address=cfg.webhook_ip or None,
        )
        logger.info(
            "Telegram webhook set: %s ip=%s",
            url,
            cfg.webhook_ip or "(dns)",
        )

    try:
        await stop_event.wait()
    finally:
        if cfg.use_webhook:
            try:
                await bot.delete_webhook(drop_pending_updates=False)
                logger.info("Telegram webhook удалён")
            except Exception:
                logger.exception("Не удалось удалить webhook")
        await runner.cleanup()
        logger.info("HTTP API остановлен")


async def run_bot_polling(
    bot: Bot, dp: Dispatcher, stop_event: asyncio.Event
) -> None:
    # На всякий случай снимаем webhook, иначе polling не получит апдейты
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.exception("delete_webhook перед polling не удался")

    polling_task = asyncio.create_task(
        dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=True,
        )
    )
    stopper = asyncio.create_task(stop_event.wait())
    done, _pending = await asyncio.wait(
        {polling_task, stopper},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if stop_event.is_set() and not polling_task.done():
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
    else:
        for task in done:
            exc = task.exception() if not task.cancelled() else None
            if exc:
                raise exc
        stopper.cancel()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    def _ask_stop() -> None:
        logger.info("Получен сигнал остановки — graceful shutdown…")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _ask_stop)
        except (NotImplementedError, RuntimeError):
            # Windows / ограниченная среда
            signal.signal(sig, lambda *_: _ask_stop())


async def main() -> None:
    global _stop_event
    try:
        config = get_config()
    except RuntimeError as exc:
        logging.basicConfig(
            level=logging.ERROR,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            stream=sys.stdout,
        )
        logging.getLogger(__name__).error("Конфигурация: %s", exc)
        raise SystemExit(1) from exc

    stop_event = asyncio.Event()
    _stop_event = stop_event

    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, stop_event)

    db = Database(
        path=config.database_path,
        database_url=config.database_url,
        debug=config.debug,
    )

    session: AiohttpSession | None = None
    if config.proxy_url:
        session = AiohttpSession(proxy=config.proxy_url)
        logger.info("Прокси установлен: %s", _mask_proxy_url(config.proxy_url))
    else:
        logger.info("Прокси не используется")

    bot = Bot(
        token=config.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(DatabaseMiddleware(db))
    register_routers(dp)

    await db.connect()
    logger.info(
        "DEBUG=%s | PORT=%s | PUBLIC_API_URL=%s | CORS=%s | DB=%s",
        config.debug,
        config.port,
        config.public_api_url or "(empty)",
        config.cors_origins or "(none)",
        "postgresql" if config.database_url else f"sqlite:{config.database_path}",
    )
    logger.info("Менеджеры: %s", config.manager_ids)
    logger.info("Каталог: ручное управление (Avito sync отключён)")
    logger.info(
        "Updates: %s",
        f"webhook {config.webhook_url}" if config.use_webhook else "long polling",
    )

    try:
        if config.use_webhook:
            await run_web_server(db, bot, stop_event, dp=dp)
        else:
            await asyncio.gather(
                run_web_server(db, bot, stop_event, dp=None),
                run_bot_polling(bot, dp, stop_event),
            )
    except Exception:
        logger.exception("Критическая ошибка в main loop")
        raise
    finally:
        stop_event.set()
        await db.close()
        await bot.session.close()
        logger.info("Бот и веб-сервер остановлены")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановка по KeyboardInterrupt")
