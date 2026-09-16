from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oveo.models import UsageEvent

MICRO_USD = Decimal("1000000")


def cost_to_microusd(value: str | int | float | Decimal) -> int:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid provider cost") from exc
    if not decimal.is_finite() or decimal < 0:
        raise ValueError("invalid provider cost")
    return int((decimal * MICRO_USD).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_lifetime_cost(amount_microusd: int) -> str:
    amount = Decimal(amount_microusd) / MICRO_USD
    if amount == 0:
        return "$0.00"
    if abs(amount) < Decimal("0.01"):
        precise = f"{amount:.6f}".rstrip("0")
        if precise.endswith("."):
            precise += "00"
        return f"${precise}"
    return f"${amount:,.2f}"


async def append_usage_event(
    db: AsyncSession,
    *,
    dedupe_key: str,
    event_type: str,
    purpose: str,
    amount_microusd: int | None,
    generation_id: str | None,
    thread_id: str | None,
    requester_id: str | None,
    provider_request_id: str | None,
    provider_generation_id: str | None = None,
) -> UsageEvent | None:
    event = UsageEvent(
        provider="openrouter",
        provider_request_id=provider_request_id,
        provider_generation_id=provider_generation_id,
        dedupe_key=dedupe_key,
        event_type=event_type,
        purpose=purpose,
        amount_microusd=amount_microusd,
        generation_id=generation_id,
        thread_id=thread_id,
        requester_id=requester_id,
    )
    try:
        async with db.begin_nested():
            db.add(event)
            await db.flush()
    except IntegrityError:
        return None
    return event


async def lifetime_total(db: AsyncSession) -> int:
    total = await db.scalar(
        select(func.coalesce(func.sum(UsageEvent.amount_microusd), 0)).where(
            UsageEvent.event_type.in_(("charge", "adjustment"))
        )
    )
    return int(total or 0)
