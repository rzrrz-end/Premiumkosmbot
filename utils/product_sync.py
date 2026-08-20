"""
Каталог ведётся вручную в Mini App.
Avito sync отключён — функции оставлены как безопасные заглушки.
"""

from __future__ import annotations

import logging

from config import Config
from database import Database

logger = logging.getLogger(__name__)


async def sync_products_from_avito(db: Database, config: Config | None = None) -> int:
    """Avito отключён: каталог не изменяется."""
    _ = (db, config)
    logger.info("Avito sync отключён — каталог не изменяется")
    return 0


async def periodic_sync(
    db: Database,
    stop_event,
    interval_minutes: int | None = None,
) -> None:
    """Фоновый sync отключён."""
    _ = (db, stop_event, interval_minutes)
    logger.info("periodic_sync отключён (Avito removed)")
    return
