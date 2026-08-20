# Telegram Mini App — магазин мужской косметики

Асинхронный бот (**aiogram 3**) и веб Mini App: каталог, бренды, корзина, заказы, лояльность, чат клиент ↔ менеджер, админка для менеджеров.

## Стек

- Python 3.11+, aiogram 3, aiohttp
- SQLite (или PostgreSQL через `DATABASE_URL`)
- Telegram Mini App (HTML/JS), Nginx + systemd на VPS

## Возможности

- Каталог с категориями и брендами (логотипы)
- Поиск, избранное, корзина
- Оформление заказа с баллами лояльности
- Админка в Mini App: товары, категории, бренды
- Панель менеджера в боте, чат с клиентом
- Webhook или long polling

## Быстрый старт

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env   # заполните BOT_TOKEN, MANAGER_IDS, PAYMENT_DETAILS, URL
python main.py
```

Откройте `MINI_APP_URL` / локальный сервер и проверьте `/health`.

## Переменные окружения

Скопируйте `.env.example` → `.env`. Обязательно:

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | токен от @BotFather |
| `MANAGER_IDS` | Telegram ID менеджеров через запятую |
| `PAYMENT_DETAILS` | реальные реквизиты оплаты |
| `MINI_APP_URL` / `PUBLIC_API_URL` / `CORS_ORIGIN` | HTTPS-хост Mini App и API |

В `mini-app.html` поле `PUBLIC_API_URL` по умолчанию пустое: при открытии с того же хоста используется `location.origin`. При отдельном API — пропишите URL вручную.

**Не коммитьте `.env`, базы `*.db`, токены и пароли.**

## Структура

```
├── main.py              # бот + HTTP API
├── config.py
├── database.py / shop_db.py / models.py
├── api_shop.py          # REST для Mini App
├── order_transactions.py
├── mini-app.html        # фронт Mini App
├── handlers/            # команды и FSM
├── keyboards/
├── utils/
├── static/              # картинки товаров, брендов, категорий
├── .env.example
└── requirements.txt
```

## Деплой (кратко)

1. Клонировать репозиторий на VPS
2. Создать `.env` из примера
3. `pip install -r requirements-prod.txt` (или `requirements.txt`)
4. systemd-сервис на `python main.py`, Nginx → порт `PORT` (обычно 8080)
5. HTTPS (Let's Encrypt / sslip.io), `USE_WEBHOOK=true`, секрет webhook

## Лицензия

Код для портфолио / личного использования. Товарные фото и логотипы брендов — права правообладателей; при публикации учитывайте это.
