from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import PriceQueryGroup


def normalize_price_query_group(name: str) -> tuple[str, str]:
    clean = str(name or "").strip()
    if not 1 <= len(clean) <= 64:
        raise ValueError("报价分组名称长度必须为 1 至 64 个字符")
    return clean, clean.casefold()


class PriceQueryGroupRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list(self) -> list[PriceQueryGroup]:
        return list((await self.session.execute(
            select(PriceQueryGroup).order_by(PriceQueryGroup.normalized_name)
        )).scalars())

    async def get(self, name: str) -> PriceQueryGroup | None:
        _, normalized = normalize_price_query_group(name)
        return (await self.session.execute(
            select(PriceQueryGroup).where(PriceQueryGroup.normalized_name == normalized)
        )).scalar_one_or_none()

    async def create(
        self, name: str, normal_check_interval: int = 4,
        buy_trailing_check_interval: int = 1,
    ) -> PriceQueryGroup:
        clean, normalized = normalize_price_query_group(name)
        if await self.get(clean):
            raise ValueError("报价分组名称已存在")
        item = PriceQueryGroup(
            name=clean, normalized_name=normalized,
            normal_check_interval=normal_check_interval,
            buy_trailing_check_interval=buy_trailing_check_interval,
        )
        self.session.add(item)
        await self.session.flush()
        return item

    async def update_settings(
        self, item: PriceQueryGroup, *, name: str,
        normal_check_interval: int, buy_trailing_check_interval: int,
    ) -> PriceQueryGroup:
        item = await self.rename(item, name)
        item.normal_check_interval = normal_check_interval
        item.buy_trailing_check_interval = buy_trailing_check_interval
        await self.session.flush()
        return item

    async def rename(self, item: PriceQueryGroup, name: str) -> PriceQueryGroup:
        clean, normalized = normalize_price_query_group(name)
        existing = await self.get(clean)
        if existing is not None and existing.id != item.id:
            raise ValueError("报价分组名称已存在")
        item.name = clean
        item.normalized_name = normalized
        await self.session.flush()
        return item
