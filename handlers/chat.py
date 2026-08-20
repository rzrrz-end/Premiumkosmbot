"""
Чат клиент ↔ менеджер: пересылка сообщений и /exit_chat.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import default_state
from aiogram.types import Message, TelegramObject
from aiogram import Bot

from database import Database
from keyboards.inline import main_menu_kb, remove_kb
from models import ChatSession

logger = logging.getLogger(__name__)
router = Router(name="chat")


async def force_close_chat(
    bot: Bot,
    db: Database,
    user_id: int,
    *,
    notify_user: bool = True,
    notify_peer: bool = True,
    user_text: str = "✅ Чат с менеджером завершён.",
    peer_text: str = "ℹ️ Собеседник завершил чат.",
) -> bool:
    """
    Принудительно закрывает активный чат пользователя (клиент или менеджер).
    Уведомляет обе стороны. Повторный вызов безопасен → False.
    """
    session = await db.get_active_chat_for_user(user_id)
    if not session or not session.active:
        return False

    await db.close_chat_session(session.id)
    logger.info("force_close_chat: сессия #%s закрыта (by=%s)", session.id, user_id)

    peer_id = (
        session.manager_id if int(session.user_id) == int(user_id) else session.user_id
    )

    if notify_user:
        try:
            await bot.send_message(user_id, user_text)
        except Exception:
            logger.exception("force_close_chat: не удалось уведомить user=%s", user_id)

    if notify_peer and peer_id and int(peer_id) != int(user_id):
        try:
            await bot.send_message(peer_id, peer_text)
        except Exception:
            logger.exception("force_close_chat: не удалось уведомить peer=%s", peer_id)

    return True


# Обратная совместимость со старым именем
async def close_active_chat(
    db: Database,
    telegram_id: int,
    *,
    client_only: bool = True,
    bot: Bot | None = None,
) -> bool:
    """Молчаливое закрытие (если bot не передан) или через force_close_chat."""
    session = await db.get_active_chat_for_user(telegram_id)
    if not session or not session.active:
        return False
    if client_only and int(session.user_id) != int(telegram_id):
        return False
    if bot is not None:
        return await force_close_chat(
            bot,
            db,
            telegram_id,
            notify_user=True,
            notify_peer=True,
        )
    await db.close_chat_session(session.id)
    return True


class ActiveChatFilter(BaseFilter):
    """Срабатывает только если у пользователя есть активная чат-сессия."""

    async def __call__(
        self,
        event: TelegramObject,
        db: Database,
    ) -> bool | dict:
        if not isinstance(event, Message) or not event.from_user:
            return False
        if event.text and event.text.startswith("/"):
            return False
        session = await db.get_active_chat_for_user(event.from_user.id)
        if not session or not session.active:
            return False
        return {"chat_session": session}


@router.message(Command("exit_chat"))
async def exit_chat(message: Message, state: FSMContext, db: Database) -> None:
    """Завершает активный чат для клиента или менеджера."""
    try:
        await state.clear()
        closed = await force_close_chat(
            message.bot,
            db,
            message.from_user.id,
            notify_user=True,
            notify_peer=True,
            user_text="✅ Чат завершён.",
            peer_text="ℹ️ Собеседник завершил чат (/exit_chat).",
        )
        if not closed:
            await message.answer("У вас нет активного чата.")
            return
        # Снять старую reply-клавиатуру и показать инлайн-меню
        await message.answer("Главное меню:", reply_markup=remove_kb())
        await message.answer("Выберите действие:", reply_markup=main_menu_kb())
    except Exception:
        logger.exception("Ошибка /exit_chat")
        await message.answer("Не удалось завершить чат.")


@router.message(
    StateFilter(default_state),
    ActiveChatFilter(),
)
async def relay_chat_message(
    message: Message,
    db: Database,
    chat_session: ChatSession,
) -> None:
    """Пересылает текст и медиа между клиентом и менеджером при active=True."""
    try:
        session = chat_session

        if message.from_user.id == session.user_id:
            target = session.manager_id
            prefix = "👤 Клиент:"
        elif message.from_user.id == session.manager_id:
            target = session.user_id
            prefix = "🧑‍💼 Менеджер:"
        else:
            return

        caption = message.caption or ""
        text = message.text or ""
        try:
            if message.photo:
                await message.bot.send_photo(
                    target,
                    message.photo[-1].file_id,
                    caption=(f"{prefix} {caption}".strip() if caption else prefix),
                )
            elif message.document:
                await message.bot.send_document(
                    target,
                    message.document.file_id,
                    caption=f"{prefix} {caption}".strip(),
                )
            elif message.video:
                await message.bot.send_video(
                    target,
                    message.video.file_id,
                    caption=f"{prefix} {caption}".strip(),
                )
            elif message.voice:
                await message.bot.send_message(target, f"{prefix} 🎤 голосовое")
                await message.bot.send_voice(target, message.voice.file_id)
            elif message.audio:
                await message.bot.send_audio(
                    target,
                    message.audio.file_id,
                    caption=f"{prefix} {caption}".strip(),
                )
            elif message.sticker:
                await message.bot.send_message(target, f"{prefix} стикер")
                await message.bot.send_sticker(target, message.sticker.file_id)
            elif text:
                await message.bot.send_message(target, f"{prefix} {text}")
            elif caption:
                await message.bot.send_message(target, f"{prefix} {caption}")
            else:
                await message.answer(
                    "Этот тип сообщения пока не пересылается. Отправьте текст или фото."
                )
                return
        except Exception:
            logger.exception("Ошибка пересылки сообщения в чат %s", session.id)
            await message.answer(
                "Не удалось доставить сообщение собеседнику. "
                "Возможно, он заблокировал бота."
            )
    except Exception:
        logger.exception("Ошибка в relay_chat_message")
