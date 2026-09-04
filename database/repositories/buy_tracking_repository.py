from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import BuyTrackingSnapshot


class BuyTrackingRepository:
    """Persistence for the short-lived Microduck buy-tracking chart."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def save(self, bot_name: str, controller_id: str, data: dict[str, Any]) -> None:
        self.session.add(BuyTrackingSnapshot(
            bot_name=bot_name,
            controller_id=controller_id,
            current_price_usd=Decimal(str(data["current_price_usd"])),
            trough_price_usd=Decimal(str(data["trough_price_usd"])),
            expected_buy_price_usd=Decimal(str(data["expected_buy_price_usd"])),
            buy_drawdown_percent=Decimal(str(data["buy_drawdown_percent"])),
            current_rebound_percent=Decimal(str(data["current_rebound_percent"])),
            maximum_rebound_percent=Decimal(str(data["maximum_rebound_percent"])),
            expected_buy_drawdown_percent=Decimal(str(data["expected_buy_drawdown_percent"])),
        ))

    async def purge_expired(self) -> None:
        await self.session.execute(delete(BuyTrackingSnapshot).where(
            BuyTrackingSnapshot.timestamp < datetime.now(timezone.utc) - timedelta(hours=24)
        ))

    async def history(self, bot_name: str, controller_id: str | None, start: datetime, limit: int = 900) -> list[dict[str, Any]]:
        query = select(BuyTrackingSnapshot).where(
            BuyTrackingSnapshot.bot_name == bot_name,
            BuyTrackingSnapshot.timestamp >= start,
        ).order_by(BuyTrackingSnapshot.timestamp.asc())
        if controller_id:
            query = query.where(BuyTrackingSnapshot.controller_id == controller_id)
        rows = list((await self.session.execute(query)).scalars())
        stride = max(1, (len(rows) + limit - 1) // limit)
        sampled = rows[::stride]
        if rows and sampled[-1].id != rows[-1].id:
            sampled.append(rows[-1])
        return [{
            "timestamp": row.timestamp.isoformat(), "bot_name": row.bot_name,
            "controller_id": row.controller_id,
            "current_price_usd": float(row.current_price_usd),
            "trough_price_usd": float(row.trough_price_usd),
            "expected_buy_price_usd": float(row.expected_buy_price_usd),
            "buy_drawdown_percent": float(row.buy_drawdown_percent),
            "current_rebound_percent": float(row.current_rebound_percent),
            "maximum_rebound_percent": float(row.maximum_rebound_percent),
            "expected_buy_drawdown_percent": float(row.expected_buy_drawdown_percent),
        } for row in sampled]
