"""
Загрузка конфигурации из переменных окружения (.env).

Критичные переменные для Render / GitHub Pages:
  PORT, BOT_TOKEN, PUBLIC_API_URL, CORS_ORIGIN, DATABASE_URL, DEBUG
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _parse_manager_ids(raw: str) -> list[int]:
    """Парсит строку вида '123,456,789' в список int."""
    if not raw or not raw.strip():
        return []
    result: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            logger.warning("Некорректный ID менеджера в MANAGER_IDS: %r", part)
    return result


def _parse_csv(raw: str) -> list[str]:
    return [p.strip().rstrip("/") for p in (raw or "").split(",") if p.strip()]


@dataclass
class Config:
    """Конфигурация приложения."""

    bot_token: str
    manager_ids: list[int]
    payment_details: str
    mini_app_url: str
    database_path: str
    proxy_url: str | None = None
    # Render задаёт PORT; fallback — 8080 (локально можно WEB_SERVER_PORT)
    port: int = 8080
    public_api_url: str = ""
    database_url: str = ""
    debug: bool = False
    # Разрешённые CORS origin
    cors_origins: list[str] = field(default_factory=list)
    # Telegram updates: webhook (прод) или long polling (локально)
    use_webhook: bool = False
    webhook_path: str = "/telegram/webhook"
    webhook_secret: str = ""
    webhook_ip: str = ""

    @property
    def avito_configured(self) -> bool:
        """Avito отключён навсегда — всегда False (совместимость)."""
        return False

    @property
    def webhook_url(self) -> str:
        base = (self.public_api_url or self.mini_app_url or "").rstrip("/")
        path = self.webhook_path if self.webhook_path.startswith("/") else f"/{self.webhook_path}"
        return f"{base}{path}"


def get_proxy() -> str | None:
    """Возвращает URL прокси из PROXY_URL или None."""
    raw = os.getenv("PROXY_URL", "").strip()
    return raw or None


def _resolve_port() -> int:
    """PORT (Render) → WEB_SERVER_PORT (legacy) → 8080."""
    for key in ("PORT", "WEB_SERVER_PORT"):
        raw = os.getenv(key, "").strip()
        if not raw:
            continue
        try:
            port = int(raw)
        except ValueError:
            logger.warning("%s=%r некорректен", key, raw)
            continue
        if 1 <= port <= 65535:
            return port
        logger.warning("%s=%s вне диапазона", key, port)
    return 8080


def _as_origin(value: str) -> str:
    """https://user.github.io/repo/ → https://user.github.io"""
    from urllib.parse import urlparse

    raw = (value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return value.strip().rstrip("/")


def _resolve_cors_origins(mini_app_url: str) -> list[str]:
    """
    CORS_ORIGIN (один) и/или CORS_ORIGINS (через запятую).
    Если пусто — берём origin из MINI_APP_URL (GitHub Pages).
    """
    origins = [_as_origin(o) for o in _parse_csv(os.getenv("CORS_ORIGIN", ""))]
    origins.extend(_as_origin(o) for o in _parse_csv(os.getenv("CORS_ORIGINS", "")))
    seen: set[str] = set()
    unique: list[str] = []
    for o in origins:
        if o and o not in seen:
            seen.add(o)
            unique.append(o)
    if not unique and mini_app_url and "example.com" not in mini_app_url:
        origin = _as_origin(mini_app_url)
        if origin:
            unique = [origin]
    return unique


def _normalize_public_api_url(raw: str) -> str:
    """PUBLIC_API_URL без завершающего слеша; пустая строка = same-origin."""
    value = (raw or "").strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    # ещё раз без хвостового /
    return value.rstrip("/")


def load_config() -> Config:
    """Читает и валидирует настройки из окружения."""
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError(
            "Не задан BOT_TOKEN. Скопируйте .env.example в .env и укажите токен."
        )

    manager_ids = _parse_manager_ids(os.getenv("MANAGER_IDS", ""))
    if not manager_ids:
        logger.warning(
            "MANAGER_IDS пуст — панель менеджера и уведомления о заказах недоступны."
        )

    payment_details = os.getenv(
        "PAYMENT_DETAILS",
        "Укажите PAYMENT_DETAILS в .env",
    ).strip()
    pay_l = payment_details.lower()
    is_stub = (
        not payment_details
        or "0000" in payment_details
        or "заглушка" in pay_l
        or "укажите payment_details" in pay_l
        or "иванов и.и" in pay_l
        or "иванов" in pay_l and "0000" in payment_details
    )
    allow_stub = _parse_bool(os.getenv("ALLOW_PAYMENT_STUB"), default=False)
    debug = _parse_bool(os.getenv("DEBUG"), default=False)
    if is_stub and not debug and not allow_stub:
        raise RuntimeError(
            "PAYMENT_DETAILS похож на заглушку. Укажите реальные реквизиты в .env "
            "перед запуском (или DEBUG=true / ALLOW_PAYMENT_STUB=true только для теста)."
        )
    if is_stub:
        logger.warning(
            "PAYMENT_DETAILS похож на заглушку (%r). "
            "Не используйте это в продакшене с реальными заказами!",
            payment_details[:80],
        )

    mini_app_url = os.getenv("MINI_APP_URL", "https://example.com").strip().rstrip("/")
    database_path = os.getenv("DATABASE_PATH", "bot_data.db").strip()
    public_api_url = _normalize_public_api_url(os.getenv("PUBLIC_API_URL", ""))
    database_url = os.getenv("DATABASE_URL", "").strip()
    proxy_url = get_proxy()
    port = _resolve_port()
    cors_origins = _resolve_cors_origins(mini_app_url)

    if not debug and not cors_origins:
        logger.warning(
            "CORS_ORIGIN / MINI_APP_URL не заданы — API будет отклонять чужие Origin. "
            "Задайте CORS_ORIGIN перед продакшеном."
        )
    if not debug and not database_url:
        logger.warning(
            "DATABASE_URL пуст — используется SQLite (%s). "
            "Для продакшена рекомендуется PostgreSQL или регулярный бэкап файла БД.",
            database_path,
        )
    if (
        not debug
        and mini_app_url
        and "example.com" not in mini_app_url
        and not public_api_url
    ):
        logger.warning(
            "PUBLIC_API_URL пуст, а Mini App на отдельном домене — "
            "пропишите PUBLIC_API_URL в .env и в mini-app.html."
        )

    use_webhook = _parse_bool(os.getenv("USE_WEBHOOK"), default=bool(public_api_url.startswith("https://")))
    webhook_path = (os.getenv("WEBHOOK_PATH", "/telegram/webhook").strip() or "/telegram/webhook")
    if not webhook_path.startswith("/"):
        webhook_path = "/" + webhook_path
    webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()
    webhook_ip = os.getenv("WEBHOOK_IP", "").strip()
    if use_webhook and not webhook_secret:
        logger.warning(
            "USE_WEBHOOK=true, но WEBHOOK_SECRET пуст — лучше задать секрет для защиты эндпоинта."
        )
    if use_webhook and not public_api_url.startswith("https://"):
        logger.warning(
            "Webhook требует HTTPS PUBLIC_API_URL (сейчас %r).",
            public_api_url or "(empty)",
        )

    return Config(
        bot_token=bot_token,
        manager_ids=manager_ids,
        payment_details=payment_details,
        mini_app_url=mini_app_url,
        database_path=database_path,
        proxy_url=proxy_url,
        port=port,
        public_api_url=public_api_url,
        database_url=database_url,
        debug=debug,
        cors_origins=cors_origins,
        use_webhook=use_webhook,
        webhook_path=webhook_path,
        webhook_secret=webhook_secret,
        webhook_ip=webhook_ip,
    )


config: Config | None = None


def get_config() -> Config:
    """Возвращает загруженную конфигурацию."""
    global config
    if config is None:
        config = load_config()
    return config
