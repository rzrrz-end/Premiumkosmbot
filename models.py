"""
Модели данных (dataclass) для пользователей, заказов и чат-сессий.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class OrderItem:
    """Одна позиция заказа."""

    name: str
    quantity: int
    price: int  # цена за единицу, руб.
    product_id: int | None = None

    @property
    def line_total(self) -> int:
        return self.quantity * self.price

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "quantity": self.quantity,
            "price": self.price,
        }
        if self.product_id is not None:
            data["id"] = int(self.product_id)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OrderItem:
        pid_raw = data.get("id", data.get("product_id"))
        product_id: int | None
        try:
            product_id = int(pid_raw) if pid_raw is not None else None
        except (TypeError, ValueError):
            product_id = None
        return cls(
            name=str(data["name"]),
            quantity=int(data["quantity"]),
            price=int(data["price"]),
            product_id=product_id,
        )


@dataclass
class User:
    """Пользователь бота / мини-приложения."""

    id: int
    telegram_id: int
    name: str | None = None
    phone: str | None = None
    address: str | None = None
    loyalty_points: int = 0
    lifetime_spent: int = 0
    created_at: str | None = None

    def to_api_dict(self, *, is_manager: bool = False) -> dict[str, Any]:
        from utils.loyalty import loyalty_rate_percent, loyalty_tier_title, next_tier_progress

        return {
            "id": self.id,
            "telegram_id": self.telegram_id,
            "name": self.name or "",
            "phone": self.phone or "",
            "address": self.address or "",
            "loyalty_points": int(self.loyalty_points or 0),
            "lifetime_spent": int(self.lifetime_spent or 0),
            "loyalty_rate": loyalty_rate_percent(self.lifetime_spent),
            "loyalty_tier": loyalty_tier_title(self.lifetime_spent),
            "loyalty_progress": next_tier_progress(self.lifetime_spent),
            "is_manager": bool(is_manager),
        }


@dataclass
class Order:
    """Заказ клиента."""

    id: int
    user_id: int
    items_json: str
    total: int
    delivery_cost: int
    final_total: int
    status: str = "new"
    created_at: str | None = None
    client_telegram_id: int | None = None
    client_name: str | None = None
    client_phone: str | None = None
    client_address: str | None = None
    shipping_address: str | None = None
    points_spent: int = 0
    points_earned: int = 0
    cash_paid: int = 0
    stock_held: int = 0

    @property
    def items(self) -> list[OrderItem]:
        try:
            raw = json.loads(self.items_json or "[]")
            return [OrderItem.from_dict(item) for item in raw]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return []

    @property
    def order_number(self) -> str:
        if self.created_at:
            stamp = (
                self.created_at.replace("-", "")
                .replace(":", "")
                .replace(" ", "")[:14]
            )
            return f"ORDER-{stamp}-{self.id}"
        return f"ORDER-{self.id}"

    def format_items(self) -> str:
        lines: list[str] = []
        for item in self.items:
            lines.append(
                f"• {item.name} × {item.quantity} = "
                f"{item.line_total} ₽ ({item.price} ₽/шт.)"
            )
        return "\n".join(lines) if lines else "—"

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order_number": self.order_number,
            "status": self.status,
            "total": int(self.total),
            "delivery_cost": int(self.delivery_cost),
            "final_total": int(self.final_total),
            "points_spent": int(self.points_spent or 0),
            "points_earned": int(self.points_earned or 0),
            "cash_paid": int(self.cash_paid or self.final_total),
            "shipping_address": (self.shipping_address or self.client_address or ""),
            "created_at": self.created_at,
            "items": [i.to_dict() for i in self.items],
        }


@dataclass
class ChatSession:
    """Активный или завершённый чат клиент ↔ менеджер."""

    id: int
    manager_id: int
    user_id: int
    order_id: int | None = None
    active: bool = True
    created_at: str | None = None


@dataclass
class Category:
    """Категория каталога (редактируемая)."""

    id: str
    title: str
    image_url: str = ""
    sort_order: int = 0
    active: bool = True

    def to_api_dict(self) -> dict[str, Any]:
        image = (self.image_url or "").strip()
        if image and not image.startswith(("http://", "https://", "data:")):
            image = "/" + image.lstrip("/")
        return {
            "id": self.id,
            "title": self.title,
            "image_url": image,
            "sort_order": int(self.sort_order),
            "active": bool(self.active),
        }


@dataclass
class Brand:
    """Бренд каталога (редактируемый, с фото)."""

    id: str
    name: str
    image_url: str = ""
    sort_order: int = 0
    active: bool = True

    def to_api_dict(self) -> dict[str, Any]:
        image = (self.image_url or "").strip()
        if image and not image.startswith(("http://", "https://", "data:")):
            image = "/" + image.lstrip("/")
        return {
            "id": self.id,
            "name": self.name,
            "photo": image or None,
            "image_url": image,
            "sort_order": int(self.sort_order),
            "active": bool(self.active),
        }


@dataclass
class Product:
    """Товар каталога (ручное управление)."""

    id: int
    avito_id: str
    name: str
    description: str = ""
    price: int = 0
    stock: int = 0
    category: str = ""
    brand: str = ""
    image_url: str = ""
    image_url_2: str = ""
    updated_at: str | None = None

    @staticmethod
    def _normalize_image(url: str) -> str:
        image = (url or "").strip()
        if image and not image.startswith(("http://", "https://", "data:")):
            image = "/" + image.lstrip("/")
        return image

    def to_api_dict(self) -> dict[str, Any]:
        image = self._normalize_image(self.image_url)
        return {
            "id": self.id,
            "avito_id": self.avito_id,
            "name": self.name,
            "description": self.description or "",
            "price": int(self.price),
            "stock": int(self.stock),
            "category": self.category or "",
            "brand": (self.brand or "").strip(),
            "image_url": image,
            "images": [image] if image else [],
        }


ORDER_STATUS_NEW = "new"
ORDER_STATUS_PROCESSING = "processing"
ORDER_STATUS_COMPLETED = "completed"
ORDER_STATUS_CANCELLED = "cancelled"

STATUS_LABELS: dict[str, str] = {
    ORDER_STATUS_NEW: "🆕 Новый",
    ORDER_STATUS_PROCESSING: "⏳ В обработке",
    ORDER_STATUS_COMPLETED: "✅ Завершён",
    ORDER_STATUS_CANCELLED: "❌ Отменён",
}


def items_to_json(items: list[OrderItem]) -> str:
    return json.dumps([i.to_dict() for i in items], ensure_ascii=False)


def calc_delivery(total: int) -> int:
    """Правило доставки: от 7000 ₽ — бесплатно, иначе 300 ₽."""
    return 0 if int(total) >= 7000 else 300
