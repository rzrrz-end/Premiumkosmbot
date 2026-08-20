"""
Оформление заказа: FSM-диалог, /new_order, /test_order.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from config import get_config
from database import Database
from handlers.chat import force_close_chat
from keyboards.inline import (
    add_more_items_kb,
    confirm_order_kb,
    main_menu_kb,
    manager_order_notify_kb,
)
from models import Order, OrderItem, calc_delivery, items_to_json

logger = logging.getLogger(__name__)
router = Router(name="order")


class OrderFSM(StatesGroup):
    """Состояния пошагового оформления заказа."""

    product_name = State()
    quantity = State()
    price = State()
    customer_name = State()
    phone = State()
    confirm = State()


def _draft_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    return list(data.get("items") or [])


def _format_summary(
    items: list[dict[str, Any]],
    customer_name: str,
    phone: str,
) -> str:
    order_items = [OrderItem.from_dict(i) for i in items]
    total = sum(i.line_total for i in order_items)
    delivery = calc_delivery(total)
    final = total + delivery

    lines = ["📦 <b>Сводка заказа</b>\n", "<b>Состав:</b>"]
    for item in order_items:
        lines.append(
            f"• {item.name} × {item.quantity} × {item.price} ₽ "
            f"= {item.line_total} ₽"
        )
    lines.extend(
        [
            "",
            f"Имя: {customer_name}",
            f"Телефон: {phone}",
            f"Сумма товаров: {total} ₽",
            f"Доставка: {delivery} ₽"
            + (" (бесплатно от 7000 ₽)" if delivery == 0 else ""),
            f"<b>Итого: {final} ₽</b>",
            "",
            "Подтвердите заказ:",
        ]
    )
    return "\n".join(lines)


async def start_order_dialog(message: Message, state: FSMContext) -> None:
    """Общий старт диалога оформления."""
    await state.clear()
    await state.set_state(OrderFSM.product_name)
    await state.update_data(items=[])
    await message.answer(
        "📦 Оформление заказа\n\n"
        "Введите название товара (или артикул).\n"
        "Чтобы отменить — /cancel",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("new_order"))
async def new_order(message: Message, state: FSMContext, db: Database) -> None:
    """Только для менеджеров. Клиентам — Mini App."""
    from handlers.manager import is_manager

    try:
        if not is_manager(message.from_user.id):
            await message.answer(
                "Оформление заказа доступно в каталоге мини-приложения.\n"
                "Откройте кнопку меню бота → Каталог."
            )
            return
        await force_close_chat(
            message.bot,
            db,
            message.from_user.id,
            notify_user=True,
            notify_peer=True,
        )
        await start_order_dialog(message, state)
    except Exception:
        logger.exception("Ошибка запуска оформления заказа")
        await message.answer("Не удалось начать оформление. Попробуйте позже.")


@router.message(Command("cancel"), StateFilter(OrderFSM))
async def cancel_order_fsm(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Оформление заказа отменено.",
        reply_markup=main_menu_kb(),
    )


@router.message(OrderFSM.product_name)
async def process_product_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Введите название товара текстом.")
        return
    if name.lower() == "готово":
        data = await state.get_data()
        if not _draft_items(data):
            await message.answer(
                "Список товаров пуст. Сначала добавьте хотя бы один товар."
            )
            return
        await state.set_state(OrderFSM.customer_name)
        await message.answer("Как вас зовут? Укажите имя:")
        return

    await state.update_data(current_name=name)
    await state.set_state(OrderFSM.quantity)
    await message.answer(
        f"Товар: <b>{name}</b>\nВведите количество (целое число):"
    )


@router.message(OrderFSM.quantity)
async def process_quantity(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Количество должно быть целым числом больше 0.")
        return
    await state.update_data(current_qty=int(text))
    await state.set_state(OrderFSM.price)
    await message.answer("Укажите цену за единицу (в рублях, целое число):")


@router.message(OrderFSM.price)
async def process_price(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip().replace(" ", "")
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Цена должна быть целым числом больше 0.")
        return

    data = await state.get_data()
    item = {
        "name": data["current_name"],
        "quantity": data["current_qty"],
        "price": int(text),
    }
    items = _draft_items(data)
    items.append(item)
    await state.update_data(
        items=items,
        current_name=None,
        current_qty=None,
    )
    line_total = item["quantity"] * item["price"]
    await state.set_state(OrderFSM.product_name)
    await message.answer(
        f"Добавлено: <b>{item['name']}</b> × {item['quantity']} "
        f"× {item['price']} ₽ = {line_total} ₽\n\n"
        "Введите название следующего товара, нажмите «Готово» "
        "или напишите «готово».",
        reply_markup=add_more_items_kb(),
    )


@router.message(OrderFSM.customer_name)
async def process_customer_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Укажите корректное имя (минимум 2 символа).")
        return
    await state.update_data(customer_name=name)
    await state.set_state(OrderFSM.phone)
    await message.answer("Укажите номер телефона:")


@router.message(OrderFSM.phone)
async def process_phone(message: Message, state: FSMContext) -> None:
    phone = (message.text or "").strip()
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 10:
        await message.answer(
            "Укажите корректный телефон (не меньше 10 цифр)."
        )
        return

    await state.update_data(phone=phone)
    data = await state.get_data()
    summary = _format_summary(
        _draft_items(data),
        data["customer_name"],
        phone,
    )
    await state.set_state(OrderFSM.confirm)
    await message.answer(summary, reply_markup=confirm_order_kb())


async def finalize_order(
    *,
    db: Database,
    telegram_id: int,
    customer_name: str,
    phone: str,
    items: list[dict[str, Any]],
) -> tuple[Order, int, int, int]:
    """Сохраняет заказ и возвращает (order, total, delivery, final)."""
    order_items = [OrderItem.from_dict(i) for i in items]
    total = sum(i.line_total for i in order_items)
    delivery = calc_delivery(total)
    final = total + delivery

    user = await db.get_or_create_user(
        telegram_id=telegram_id,
        name=customer_name,
        phone=phone,
    )
    order = await db.create_order(
        user_id=user.id,
        items_json=items_to_json(order_items),
        total=total,
        delivery_cost=delivery,
        final_total=final,
        status="new",
    )
    return order, total, delivery, final


async def notify_managers_about_order(
    bot: Bot,
    order: Order,
    customer_name: str,
    phone: str,
    address: str = "",
) -> None:
    """Рассылает карточку заказа всем менеджерам."""
    import html

    config = get_config()
    client_tg = order.client_telegram_id
    addr = (address or order.client_address or "").strip() or "—"
    text = (
        f"🛍 <b>Новый заказ {html.escape(order.order_number)}</b>\n\n"
        f"Имя: {html.escape(customer_name)}\n"
        f"Телефон: {html.escape(phone)}\n"
        f"Адрес: {html.escape(addr)}\n"
        f"Telegram ID: {client_tg}\n\n"
        f"<b>Состав:</b>\n{order.format_items()}\n\n"
        f"Сумма товаров: {order.total} ₽\n"
        f"Доставка: {order.delivery_cost} ₽\n"
        f"Баллы списано: {int(order.points_spent or 0)}\n"
        f"К оплате: {int(order.cash_paid or order.final_total)} ₽\n"
        f"<b>Итого (до баллов): {order.final_total} ₽</b>"
    )
    kb = manager_order_notify_kb(order.id, client_tg or 0)
    for manager_id in config.manager_ids:
        try:
            await bot.send_message(manager_id, text, reply_markup=kb)
        except Exception:
            logger.exception(
                "Не удалось отправить уведомление менеджеру %s", manager_id
            )


async def notify_customer_about_payment(
    bot: Bot,
    *,
    telegram_id: int,
    order: Order,
) -> None:
    """Отправляет клиенту реквизиты и сумму к оплате в личку бота."""
    import html

    config = get_config()
    cash = int(order.cash_paid or order.final_total or 0)
    details = (config.payment_details or "").strip() or "Уточните реквизиты у менеджера"
    text = (
        f"✅ Заказ <b>{html.escape(order.order_number)}</b> оформлен!\n\n"
        f"Сумма товаров: {order.total} ₽\n"
        f"Доставка: {order.delivery_cost} ₽\n"
        f"Списано баллов: {int(order.points_spent or 0)}\n"
        f"<b>К оплате: {cash} ₽</b>\n\n"
        f"💳 <b>Реквизиты для оплаты:</b>\n"
        f"<code>{html.escape(details)}</code>\n\n"
        "После оплаты менеджер свяжется с вами.\n"
        "Если нужна помощь — напишите через «Связаться с менеджером»."
    )
    try:
        await bot.send_message(telegram_id, text, reply_markup=main_menu_kb())
    except Exception:
        logger.exception(
            "Не удалось отправить реквизиты клиенту %s по заказу %s",
            telegram_id,
            order.id,
        )


@router.message(Command("test_order"))
async def test_order(message: Message, state: FSMContext, db: Database) -> None:
    """Быстрое создание тестового заказа без диалога. Только DEBUG + менеджер."""
    from handlers.manager import is_manager

    config = get_config()
    if not config.debug:
        await message.answer("Команда недоступна.")
        return
    if not is_manager(message.from_user.id):
        await message.answer("Только для менеджеров.")
        return
    try:
        await state.clear()
        items = [
            {"name": "Тестовый крем", "quantity": 2, "price": 1500},
            {"name": "Тестовая сыворотка", "quantity": 1, "price": 2000},
        ]
        name = message.from_user.full_name or "Тест"
        phone = "+70000000000"
        order, total, delivery, final = await finalize_order(
            db=db,
            telegram_id=message.from_user.id,
            customer_name=name,
            phone=phone,
            items=items,
        )
        await message.answer(
            f"✅ Тестовый заказ создан: <b>{order.order_number}</b>\n"
            f"Сумма: {total} ₽ + доставка {delivery} ₽ = <b>{final} ₽</b>\n\n"
            f"💳 Реквизиты для оплаты:\n{config.payment_details}",
            reply_markup=main_menu_kb(),
        )
        await notify_managers_about_order(message.bot, order, name, phone)
    except Exception:
        logger.exception("Ошибка /test_order")
        await message.answer("Не удалось создать тестовый заказ.")
