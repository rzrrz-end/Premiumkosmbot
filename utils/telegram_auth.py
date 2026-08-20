"""
Проверка подписи Telegram WebApp initData.
Документация: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

logger = logging.getLogger(__name__)


@dataclass
class WebAppUser:
    id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None


@dataclass
class ValidatedInitData:
    user: WebAppUser
    auth_date: int
    raw: dict[str, str]


class InitDataError(ValueError):
    """Невалидные или просроченные initData."""


def validate_webapp_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_sec: int = 3600,
) -> ValidatedInitData:
    """
    Проверяет HMAC-подпись initData и возвращает данные пользователя.
    max_age_sec по умолчанию 1 час.
    """
    if not init_data or not str(init_data).strip():
        raise InitDataError("initData пуст")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise InitDataError("В initData нет hash")

    # data_check_string
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))

    secret_key = hmac.new(
        key=b"WebAppData",
        msg=bot_token.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    calculated = hmac.new(
        key=secret_key,
        msg=data_check_string.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        raise InitDataError("Неверная подпись initData")

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as exc:
        raise InitDataError("Некорректный auth_date") from exc

    if auth_date <= 0:
        raise InitDataError("Некорректный auth_date")

    if max_age_sec > 0:
        age = int(time.time()) - auth_date
        if age < -60:
            raise InitDataError("auth_date из будущего")
        if age > max_age_sec:
            raise InitDataError("initData устарел")

    user_raw = pairs.get("user")
    if not user_raw:
        raise InitDataError("В initData нет user")

    try:
        user_obj = json.loads(user_raw)
        user_id = int(user_obj["id"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise InitDataError("Некорректный user в initData") from exc

    user = WebAppUser(
        id=user_id,
        first_name=user_obj.get("first_name"),
        last_name=user_obj.get("last_name"),
        username=user_obj.get("username"),
    )
    return ValidatedInitData(user=user, auth_date=auth_date, raw=pairs)
