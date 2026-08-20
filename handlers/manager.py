"""
Панель менеджера: /manager.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import get_config
from database import Database
from keyboards.inline import format_status, manager_panel_kb, order_card_kb

logger = logging.getLogger(__name__)
router = Router(name="manager")


def is_manager(user_id: int) -> bool:
    return user_id in get_config().manager_ids


@router.message(Command("manager"))
async def cmd_manager(message: Message) -> None:
    """Панель менеджера — только для MANAGER_IDS."""
    try:
        if not is_manager(message.from_user.id):
            await message.answer("⛔ Доступ запрещён. Команда только для менеджеров.")
            return

        await message.answer(
            "🧑‍💼 <b>Панель менеджера</b>\n"
            "Новые заказы, активные чаты и общий список — кнопки ниже.\n"
            "Каталог редактируется в мини-приложении (Аккаунт → Админ).",
            reply_markup=manager_panel_kb(),
        )
    except Exception:
        logger.exception("Ошибка /manager")
        await message.answer("Не удалось открыть панель менеджера.")


@router.message(Command("sync"))
async def cmd_sync(message: Message) -> None:
    """Avito отключён — каталог ведётся вручную."""
    if not is_manager(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    await message.answer(
        "Каталог больше не синхронизируется с Avito.\n"
        "Добавляйте и правьте товары в мини-приложении: Аккаунт → Админ."
    )


@router.message(Command("clean_test"))
async def cmd_clean_test(message: Message, db: Database) -> None:
    """Удаляет demo / тестовые товары (только менеджеры)."""
    if not is_manager(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    try:
        removed = await db.delete_test_products()
        await message.answer(f"Удалено тестовых товаров: <b>{removed}</b>")
    except Exception:
        logger.exception("Ошибка /clean_test")
        await message.answer("❌ Не удалось очистить тестовые товары.")

async def send_orders_list(
    message: Message,
    db: Database,
    *,
    status: str | None = None,
    title: str,
) -> None:
    """Отправляет список заказов менеджеру."""
    orders = await db.get_all_orders(status=status)
    if not orders:
        await message.answer(f"{title}\n\nЗаказов нет.")
        return

    await message.answer(f"{title}\nВсего: {len(orders)}")
    for order in orders[:30]:
        text = (
            f"<b>{order.order_number}</b> — {format_status(order.status)}\n"
            f"Клиент: {order.client_name or '—'} "
            f"(tg: {order.client_telegram_id})\n"
            f"Телефон: {order.client_phone or '—'}\n"
            f"Адрес: {order.client_address or '—'}\n"
            f"{order.format_items()}\n"
            f"Итого: {order.final_total} ₽ "
            f"(доставка {order.delivery_cost} ₽)\n"
            f"Дата: {order.created_at or '—'}"
        )
        await message.answer(text, reply_markup=order_card_kb(order))


async def send_active_chats_list(message: Message, db: Database) -> None:
    chats = await db.get_active_chats()
    if not chats:
        await message.answer("💬 Активных чатов нет.")
        return

    lines = ["💬 <b>Активные чаты:</b>\n"]
    for chat in chats:
        lines.append(
            f"#{chat.id}: клиент <code>{chat.user_id}</code> ↔ "
            f"менеджер <code>{chat.manager_id}</code>"
            + (f", заказ #{chat.order_id}" if chat.order_id else "")
            + f"\nсоздан: {chat.created_at or '—'}\n"
        )
    await message.answer("\n".join(lines))
