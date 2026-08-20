"""
Команды /start, /help. Главное меню — инлайн-кнопки под сообщением.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from config import get_config
from database import Database
from handlers.chat import force_close_chat
from keyboards.inline import format_status, main_menu_kb, remove_kb

logger = logging.getLogger(__name__)
router = Router(name="start")


def help_text(user_id: int) -> str:
    """Справка: команды менеджера только для менеджеров."""
    from handlers.manager import is_manager

    lines = [
        "📖 <b>Справка</b>\n",
        "/start — главное меню",
        "/help — эта справка",
        "/exit_chat — завершить чат с менеджером",
    ]
    if is_manager(user_id):
        lines.extend(
            [
                "",
                "<b>Для менеджеров</b>",
                "/manager — панель менеджера",
                "Каталог — в мини-приложении (Аккаунт → Админ)",
            ]
        )
    lines.extend(
        [
            "",
            "Заказы оформляются в мини-приложении («Каталог»).",
        ]
    )
    return "\n".join(lines)


def _start_text(_first_name: str = "") -> str:
    return (
        "🛍 Добро пожаловать в магазин «Премиальная косметика»\n\n"
        "В каталоге вы можете ознакомиться с ассортиментом, актуальными ценами "
        "и наличием товаров, а также оформить заказ прямо в Telegram.\n\n"
        "🚚 Доставка — <b>300 ₽</b>\n"
        "При заказе от <b>7 000 ₽</b> — бесплатно.\n\n"
        "Если вам понадобится помощь с выбором или оформлением заказа, "
        "наш менеджер подключится к диалогу и ответит на ваши вопросы.\n\n"
        "Выберите действие:"
    )


def format_my_orders_text(orders: list) -> str:
    """Текст списка заказов для клиента."""
    if not orders:
        return (
            "У вас пока нет заказов.\n"
            "Оформить можно через кнопку «Каталог»."
        )
    lines: list[str] = ["📋 <b>Ваши заказы:</b>\n"]
    for order in orders[:20]:
        lines.append(
            f"<b>{order.order_number}</b> — {format_status(order.status)}\n"
            f"Сумма: {order.final_total} ₽ "
            f"(товары {order.total} + доставка {order.delivery_cost})\n"
            f"Дата: {order.created_at or '—'}\n"
        )
    return "\n".join(lines)


async def start_contact_with_manager(
    *,
    bot,
    db: Database,
    client_id: int,
    client_name: str,
    answer,
) -> None:
    """
    Создаёт чат клиент ↔ менеджер.
    answer(text) — корутина отправки ответа пользователю
    (message.answer или callback.message.answer).
    """
    config = get_config()
    if not config.manager_ids:
        await answer("Сейчас нет доступных менеджеров. Попробуйте позже.")
        return

    busy = await db.get_managers_with_active_chats()
    free = [mid for mid in config.manager_ids if mid not in busy]
    manager_id = free[0] if free else config.manager_ids[0]

    await db.get_or_create_user(telegram_id=client_id, name=client_name)
    session = await db.create_chat_session(
        manager_id=manager_id,
        user_id=client_id,
        order_id=None,
    )

    await answer(
        "📞 Запрос отправлен. Менеджер подключён к чату.\n"
        "Пишите сообщения — они будут пересланы.\n"
        "Для выхода: /exit_chat"
    )

    try:
        await bot.send_message(
            manager_id,
            f"📩 Клиент <b>{client_name}</b> (id={client_id}) "
            f"хочет связаться.\n"
            f"Сессия #{session.id}.\n"
            "Сообщения клиента будут пересылаться вам. "
            "Для выхода: /exit_chat",
        )
    except Exception:
        logger.exception(
            "Не удалось уведомить менеджера %s о новом чате", manager_id
        )
        await answer(
            "Не удалось связаться с менеджером прямо сейчас. "
            "Попробуйте позже или оформите заказ в магазине."
        )
        await db.close_chat_session(session.id)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db: Database) -> None:
    """Приветствие и инлайн-меню."""
    try:
        await state.clear()
        # Закрыть диалог с менеджером при входе в меню
        await force_close_chat(
            message.bot,
            db,
            message.from_user.id,
            notify_user=True,
            notify_peer=True,
        )
        await db.get_or_create_user(
            telegram_id=message.from_user.id,
            name=message.from_user.full_name,
        )
        # Снять старую reply-клавиатуру (нужен непустой текст), затем удалить служебное сообщение
        stub = await message.answer("Загрузка меню…", reply_markup=remove_kb())
        await message.answer(_start_text(), reply_markup=main_menu_kb())
        try:
            await stub.delete()
        except Exception:
            pass
    except Exception:
        logger.exception("Ошибка в /start")
        await message.answer("Произошла ошибка. Попробуйте позже.")


@router.message(Command("help"))
async def cmd_help(message: Message, db: Database) -> None:
    await force_close_chat(
        message.bot,
        db,
        message.from_user.id,
        notify_user=True,
        notify_peer=True,
    )
    await message.answer(
        help_text(message.from_user.id),
        reply_markup=main_menu_kb(),
    )
