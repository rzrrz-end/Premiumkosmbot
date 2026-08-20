"""Программа лояльности: % кэшбэка от накопленной суммы покупок."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LoyaltyTier:
    min_spent: int
    rate_percent: int
    title: str


# Порог → процент начисления с суммы покупки (денежной части)
LOYALTY_TIERS: tuple[LoyaltyTier, ...] = (
    LoyaltyTier(50_000, 5, "5%"),
    LoyaltyTier(20_000, 3, "3%"),
    LoyaltyTier(10_000, 2, "2%"),
    LoyaltyTier(0, 1, "1%"),
)


def loyalty_rate_percent(lifetime_spent: int) -> int:
    """Процент кэшбэка по накопленным покупкам (руб.)."""
    spent = max(0, int(lifetime_spent or 0))
    for tier in LOYALTY_TIERS:
        if spent >= tier.min_spent:
            return tier.rate_percent
    return 1


def loyalty_tier_title(lifetime_spent: int) -> str:
    rate = loyalty_rate_percent(lifetime_spent)
    for tier in LOYALTY_TIERS:
        if tier.rate_percent == rate:
            return tier.title
    return "1%"


def calc_points_earn(*, cash_paid: int, lifetime_spent_before: int) -> int:
    """Баллы = floor(оплачено деньгами * rate%)."""
    cash = max(0, int(cash_paid or 0))
    if cash <= 0:
        return 0
    rate = loyalty_rate_percent(lifetime_spent_before)
    return (cash * rate) // 100


def next_tier_progress(lifetime_spent: int) -> dict[str, int | str | None]:
    """Сколько до следующего порога."""
    spent = max(0, int(lifetime_spent or 0))
    rate = loyalty_rate_percent(spent)
    # следующий более высокий порог
    higher = sorted(
        [t for t in LOYALTY_TIERS if t.rate_percent > rate],
        key=lambda t: t.min_spent,
    )
    if not higher:
        return {
            "current_rate": rate,
            "next_rate": None,
            "next_min": None,
            "remaining": 0,
        }
    nxt = higher[0]
    return {
        "current_rate": rate,
        "next_rate": nxt.rate_percent,
        "next_min": nxt.min_spent,
        "remaining": max(0, nxt.min_spent - spent),
    }
