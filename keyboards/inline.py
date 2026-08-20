"""
Инлайн-клавиатуры бота (главное меню — под сообщением).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)

from models import Order, STATUS_LABELS


def remove_kb() -> ReplyKeyboardRemove:
    """Снять старую reply-клавиатуру (миграция с прежнего меню)."""
    return ReplyKeyboardRemove()


def main_menu_kb() -> InlineKeyboardMarkup:
    """Главное меню: каталог (WebApp) + заказы + менеджер."""
    return main_menu_keyboard()


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Инлайн-меню под сообщением /start."""
    from config import get_config

    rows: list[list[InlineKeyboardButton]] = []
    mini_url = (get_config().mini_app_url or "").strip()
    if mini_url:
        # Cache-bust для Telegram Mini App:
        # при изменении mini-app.html меняем query-параметр v,
        # чтобы клиент подтянул свежую версию интерфейса.
        try:
            html_path = Path(__file__).resolve().parents[1] / "mini-app.html"
            version = str(int(html_path.stat().st_mtime))
            u = urlsplit(mini_url)
            q = dict(parse_qsl(u.query, keep_blank_values=True))
            q["v"] = version
            mini_url = urlunsplit((u.scheme, u.netloc, u.path, urlencode(q), u.fragment))
        except Exception:
            pass
    if mini_url and "example.com" not in mini_url:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🛍  Каталог",
                    web_app=WebAppInfo(url=mini_url),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="📦  Мои заказы",
                callback_data="my_orders",
            ),
            InlineKeyboardButton(
                text="💬  Менеджер",
                callback_data="contact_manager",
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="ℹ️  Справка",
                callback_data="menu_help",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_order_kb() -> InlineKeyboardMarkup:
    """Подтверждение / отмена заказа."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅  Подтвердить",
                    callback_data="order_confirm",
                ),
                InlineKeyboardButton(
                    text="❌  Отменить",
                    callback_data="order_cancel",
                ),
            ]
        ]
    )


def add_more_items_kb() -> InlineKeyboardMarkup:
    """Добавить ещё товар или завершить ввод позиций."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕  Ещё товар",
                    callback_data="order_add_more",
                ),
                InlineKeyboardButton(
                    text="✅  Готово",
                    callback_data="order_items_done",
                ),
            ]
        ]
    )


def manager_panel_kb() -> InlineKeyboardMarkup:
    """Панель менеджера."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📩  Новые",
                    callback_data="mgr_new_orders",
                ),
                InlineKeyboardButton(
                    text="💬  Чаты",
                    callback_data="mgr_active_chats",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📋  Все заказы",
                    callback_data="mgr_all_orders",
                )
            ],
        ]
    )


def manager_order_notify_kb(order_id: int, client_telegram_id: int) -> InlineKeyboardMarkup:
    """Кнопки в уведомлении менеджеру о новом заказе."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬  В чат",
                    callback_data=f"chat_join:{order_id}:{client_telegram_id}",
                ),
                InlineKeyboardButton(
                    text="✅  Закрыть",
                    callback_data=f"close_order:{order_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌  Отменить",
                    callback_data=f"cancel_order:{order_id}",
                ),
            ],
        ]
    )


def order_card_kb(order: Order) -> InlineKeyboardMarkup:
    """Кнопки под карточкой заказа в панели менеджера."""
    client_id = order.client_telegram_id or 0
    buttons: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="💬  Вступить в чат",
                callback_data=f"chat_join:{order.id}:{client_id}",
            )
        ]
    ]
    if order.status not in ("completed", "cancelled"):
        buttons.append(
            [
                InlineKeyboardButton(
                    text="✅  Закрыть заказ",
                    callback_data=f"close_order:{order.id}",
                ),
                InlineKeyboardButton(
                    text="❌  Отменить",
                    callback_data=f"cancel_order:{order.id}",
                ),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def format_status(status: str) -> str:
    return STATUS_LABELS.get(status, status)
