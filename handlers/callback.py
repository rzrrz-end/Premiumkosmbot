"""
Обработка инлайн-кнопок (callback_query).
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from config import get_config
from database import Database
from handlers.chat import force_close_chat
from handlers.manager import is_manager, send_active_chats_list, send_orders_list
from handlers.order import (
    OrderFSM,
    finalize_order,
    notify_managers_about_order,
    _draft_items,
)
from handlers.start import format_my_orders_text, help_text, start_contact_with_manager
from keyboards.inline import main_menu_kb
from models import ORDER_STATUS_CANCELLED, ORDER_STATUS_COMPLETED

logger = logging.getLogger(__name__)
router = Router(name="callback")


# --------------------------------------------------------------- главное меню


@router.callback_query(F.data == "contact_manager")
async def cb_contact_manager(callback: CallbackQuery, db: Database) -> None:
    """Связь с менеджером: закрыть старый чат → открыть новый."""
    try:
        await callback.answer()
        await force_close_chat(
            callback.bot,
            db,
            callback.from_user.id,
            notify_user=True,
            notify_peer=True,
        )
        await start_contact_with_manager(
            bot=callback.bot,
            db=db,
            client_id=callback.from_user.id,
            client_name=callback.from_user.full_name or str(callback.from_user.id),
            answer=callback.message.answer,
        )
    except Exception:
        logger.exception("Ошибка callback contact_manager")
        await callback.answer("Не удалось создать чат", show_alert=True)


@router.callback_query(F.data == "my_orders")
async def cb_my_orders(callback: CallbackQuery, db: Database) -> None:
    """Список заказов клиента (с авто-закрытием чата)."""
    try:
        await callback.answer()
        await force_close_chat(
            callback.bot,
            db,
            callback.from_user.id,
            notify_user=True,
            notify_peer=True,
        )
        orders = await db.get_orders_by_telegram_id(callback.from_user.id)
        await callback.message.answer(
            format_my_orders_text(orders),
            reply_markup=main_menu_kb(),
        )
    except Exception:
        logger.exception("Ошибка callback my_orders")
        await callback.answer("Не удалось загрузить заказы", show_alert=True)


@router.callback_query(F.data == "menu_help")
async def cb_menu_help(callback: CallbackQuery, db: Database) -> None:
    """Краткая справка из главного меню."""
    await callback.answer()
    await force_close_chat(
        callback.bot,
        db,
        callback.from_user.id,
        notify_user=True,
        notify_peer=True,
    )
    await callback.message.answer(
        help_text(callback.from_user.id),
        reply_markup=main_menu_kb(),
    )


# ------------------------------------------------------------------ заказ FSM


@router.callback_query(F.data == "order_add_more", OrderFSM.product_name)
async def cb_order_add_more(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.answer("Введите название следующего товара:")


@router.callback_query(F.data == "order_items_done", OrderFSM.product_name)
async def cb_order_items_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not _draft_items(data):
        await callback.answer("Добавьте хотя бы один товар", show_alert=True)
        return
    await callback.answer()
    await state.set_state(OrderFSM.customer_name)
    await callback.message.answer("Как вас зовут? Укажите имя:")


@router.callback_query(F.data == "order_confirm", OrderFSM.confirm)
async def cb_order_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
) -> None:
    try:
        await force_close_chat(
            callback.bot,
            db,
            callback.from_user.id,
            notify_user=False,
            notify_peer=True,
        )
        data = await state.get_data()
        items = _draft_items(data)
        customer_name = data.get("customer_name")
        phone = data.get("phone")
        if not items or not customer_name or not phone:
            await callback.answer("Данные заказа неполные", show_alert=True)
            await state.clear()
            return

        order, total, delivery, final = await finalize_order(
            db=db,
            telegram_id=callback.from_user.id,
            customer_name=customer_name,
            phone=phone,
            items=items,
        )
        await state.clear()
        await callback.answer("Заказ оформлен!")

        config = get_config()
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"✅ Заказ <b>{order.order_number}</b> оформлен!\n\n"
            f"Сумма товаров: {total} ₽\n"
            f"Доставка: {delivery} ₽\n"
            f"<b>Итого к оплате: {final} ₽</b>\n\n"
            f"💳 Реквизиты для оплаты:\n{config.payment_details}\n\n"
            "После оплаты менеджер свяжется с вами.\n"
            "Нужна помощь — кнопка «Связаться с менеджером».",
            reply_markup=main_menu_kb(),
        )
        await notify_managers_about_order(
            callback.bot, order, customer_name, phone
        )
    except Exception:
        logger.exception("Ошибка подтверждения заказа")
        await callback.answer("Ошибка при создании заказа", show_alert=True)
        await state.clear()


@router.callback_query(F.data == "order_cancel", OrderFSM.confirm)
async def cb_order_cancel(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    await force_close_chat(
        callback.bot,
        db,
        callback.from_user.id,
        notify_user=False,
        notify_peer=True,
    )
    await state.clear()
    await callback.answer("Отменено")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "Заказ отменён.",
        reply_markup=main_menu_kb(),
    )


# ----------------------------------------------------------- панель менеджера


@router.callback_query(F.data == "mgr_all_orders")
async def cb_mgr_all_orders(callback: CallbackQuery, db: Database) -> None:
    if not is_manager(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    try:
        await send_orders_list(
            callback.message,
            db,
            status=None,
            title="📋 <b>Все заказы</b>",
        )
    except Exception:
        logger.exception("Ошибка mgr_all_orders")
        await callback.message.answer("Не удалось загрузить заказы.")


@router.callback_query(F.data == "mgr_new_orders")
async def cb_mgr_new_orders(callback: CallbackQuery, db: Database) -> None:
    if not is_manager(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    try:
        await send_orders_list(
            callback.message,
            db,
            status="new",
            title="📩 <b>Необработанные заказы</b>",
        )
    except Exception:
        logger.exception("Ошибка mgr_new_orders")
        await callback.message.answer("Не удалось загрузить заказы.")


@router.callback_query(F.data == "mgr_active_chats")
async def cb_mgr_active_chats(callback: CallbackQuery, db: Database) -> None:
    if not is_manager(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    try:
        await send_active_chats_list(callback.message, db)
    except Exception:
        logger.exception("Ошибка mgr_active_chats")
        await callback.message.answer("Не удалось загрузить чаты.")


# --------------------------------------------------------------- чат / заказ


@router.callback_query(F.data.startswith("chat_join:"))
async def cb_chat_join(callback: CallbackQuery, db: Database) -> None:
    """Менеджер вступает в чат с клиентом."""
    try:
        if not is_manager(callback.from_user.id):
            await callback.answer("Только для менеджеров", show_alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) != 3:
            await callback.answer("Некорректные данные", show_alert=True)
            return

        _, order_id_s, user_id_s = parts
        order_id = int(order_id_s) if order_id_s.isdigit() else None
        client_id = int(user_id_s)

        if client_id <= 0:
            await callback.answer("Неизвестный клиент", show_alert=True)
            return

        # Если заказ указан — помечаем как processing
        if order_id:
            order = await db.get_order_by_id(order_id)
            if order and order.status == "new":
                await db.update_order_status(order_id, "processing")

        session = await db.create_chat_session(
            manager_id=callback.from_user.id,
            user_id=client_id,
            order_id=order_id,
        )
        await callback.answer("Вы в чате")
        await callback.message.answer(
            "Вы вступили в чат с клиентом. Все сообщения клиента будут "
            "пересылаться вам, ваши ответы — клиенту.\n"
            "Для выхода используйте /exit_chat\n"
            f"(сессия #{session.id})"
        )
        try:
            await callback.bot.send_message(
                client_id,
                "🧑‍💼 Менеджер подключился к чату. Можете писать сообщения.\n"
                "Для завершения: /exit_chat",
            )
        except Exception:
            logger.exception("Не удалось уведомить клиента %s о чате", client_id)
    except Exception:
        logger.exception("Ошибка chat_join")
        await callback.answer("Ошибка при создании чата", show_alert=True)


@router.callback_query(F.data.startswith("close_order:"))
async def cb_close_order(callback: CallbackQuery, db: Database) -> None:
    """Менеджер закрывает заказ (status = completed) + loyalty в одной TX."""
    try:
        if not is_manager(callback.from_user.id):
            await callback.answer("Только для менеджеров", show_alert=True)
            return

        order_id_s = callback.data.split(":", 1)[1]
        if not order_id_s.isdigit():
            await callback.answer("Некорректный ID", show_alert=True)
            return

        order_id = int(order_id_s)
        order = await db.get_order_by_id(order_id)
        if not order:
            await callback.answer("Заказ не найден", show_alert=True)
            return

        if order.status == ORDER_STATUS_COMPLETED:
            await callback.answer("Заказ уже закрыт", show_alert=True)
            return
        if order.status == ORDER_STATUS_CANCELLED:
            await callback.answer("Заказ отменён", show_alert=True)
            return

        all_products = None
        deductions: list[tuple[int, int]] = []
        for item in order.items:
            pid = item.product_id
            if not pid:
                if all_products is None:
                    all_products = await db.get_all_products()
                match = next(
                    (p for p in all_products if p.name == item.name),
                    None,
                )
                pid = match.id if match else None
            if not pid:
                logger.warning(
                    "close_order #%s: нет product_id для «%s»",
                    order_id,
                    item.name,
                )
                continue
            deductions.append((int(pid), int(item.quantity)))

        ok, err, deducted, earned = await db.complete_order_with_loyalty(
            order_id, deductions
        )
        if not ok:
            if err == "already_completed":
                await callback.answer("Заказ уже закрыт", show_alert=True)
                return
            if err == "cancelled":
                await callback.answer("Заказ отменён", show_alert=True)
                return
            if err.startswith("insufficient_stock"):
                await callback.answer(
                    "Недостаточно остатка на складе — заказ не закрыт",
                    show_alert=True,
                )
                return
            await callback.answer("Не удалось закрыть заказ", show_alert=True)
            return

        await callback.answer("Заказ закрыт")
        stock_note = (
            f"Списано со склада (legacy): {deducted}."
            if deducted
            else "Склад уже был зарезервирован при оформлении."
        )
        await callback.message.answer(
            f"✅ Заказ <b>{order.order_number}</b> помечен как завершённый.\n"
            f"{stock_note}\n"
            f"Начислено баллов клиенту: {earned}."
        )

        if order.client_telegram_id:
            try:
                pts_line = (
                    f" Начислено {earned} баллов лояльности."
                    if earned > 0
                    else ""
                )
                await callback.bot.send_message(
                    order.client_telegram_id,
                    f"✅ Ваш заказ <b>{order.order_number}</b> завершён. "
                    f"Спасибо за покупку!{pts_line}",
                )
            except Exception:
                logger.exception(
                    "Не удалось уведомить клиента о закрытии заказа %s",
                    order_id,
                )
    except Exception:
        logger.exception("Ошибка close_order")
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("cancel_order:"))
async def cb_cancel_order(callback: CallbackQuery, db: Database) -> None:
    """Менеджер отменяет заказ: возврат стока и баллов."""
    try:
        if not is_manager(callback.from_user.id):
            await callback.answer("Только для менеджеров", show_alert=True)
            return

        order_id_s = callback.data.split(":", 1)[1]
        if not order_id_s.isdigit():
            await callback.answer("Некорректный ID", show_alert=True)
            return

        order_id = int(order_id_s)
        order = await db.get_order_by_id(order_id)
        if not order:
            await callback.answer("Заказ не найден", show_alert=True)
            return

        ok, err = await db.cancel_order_restore_stock(order_id)
        if not ok:
            if err == "already_completed":
                await callback.answer(
                    "Нельзя отменить завершённый заказ", show_alert=True
                )
                return
            await callback.answer("Не удалось отменить", show_alert=True)
            return

        await callback.answer("Заказ отменён")
        await callback.message.answer(
            f"❌ Заказ <b>{order.order_number}</b> отменён. "
            f"Остатки и баллы возвращены."
        )
        if order.client_telegram_id:
            try:
                await callback.bot.send_message(
                    order.client_telegram_id,
                    f"❌ Ваш заказ <b>{order.order_number}</b> отменён менеджером. "
                    f"Если уже оплатили — напишите в поддержку.",
                )
            except Exception:
                logger.exception(
                    "Не удалось уведомить клиента об отмене заказа %s",
                    order_id,
                )
    except Exception:
        logger.exception("Ошибка cancel_order")
        await callback.answer("Ошибка", show_alert=True)
