"""按报价分组复用 MICRODUCK/USDG 的日常参考报价。

这里缓存的只是策略跟踪用的市场参考报价；下单前的最终报价永远不经过此服务。
"""

import asyncio
import os
import time
from decimal import Decimal

from services.gateway_client import GatewayClient, check_gateway_error


class MicroduckQuoteService:
    def __init__(self):
        self._gateway = GatewayClient(os.getenv("GATEWAY_URL", "http://gateway:15888"))
        self._cache: dict[tuple[str, ...], dict] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(
        group: str, chain: str, network: str, dex: str, trading_type: str,
        side: str, amount: Decimal,
    ) -> tuple[str, ...]:
        # amount 纳入键，避免把不同数量、不同价格影响的卖出报价误当作同一价格。
        return (group, chain, network, dex, trading_type, side.upper(), format(amount, "f"))

    async def get_quote(
        self,
        *,
        group: str,
        chain: str,
        network: str,
        dex: str,
        trading_type: str,
        side: str,
        amount: Decimal,
        max_age_seconds: float,
    ) -> dict:
        clean_group = group.strip()
        if not clean_group:
            raise ValueError("报价分组不能为空")
        if amount <= 0:
            raise ValueError("报价数量必须大于0")
        key = self._key(clean_group, chain, network, dex, trading_type, side, amount)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and now - cached["fetched_at"] < max_age_seconds:
            return {**cached["quote"], "shared_quote": True, "shared_cache_age_seconds": round(now - cached["fetched_at"], 3)}

        async with self._lock:
            now = time.monotonic()
            cached = self._cache.get(key)
            if cached and now - cached["fetched_at"] < max_age_seconds:
                return {**cached["quote"], "shared_quote": True, "shared_cache_age_seconds": round(now - cached["fetched_at"], 3)}
            quote = check_gateway_error(await self._gateway.quote_swap(
                connector=f"{dex}/{trading_type}",
                chain_network=f"{chain}-{network}",
                base_asset="MICRODUCK",
                quote_asset="USDG",
                amount=float(amount),
                side=side,
                slippage_pct=0,
            ))
            self._cache[key] = {"quote": quote, "fetched_at": time.monotonic()}
            return {**quote, "shared_quote": True, "shared_cache_age_seconds": 0.0}


microduck_quote_service = MicroduckQuoteService()
