from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("hummingbot")

from services.microduck_quote_service import MicroduckQuoteService


@pytest.mark.asyncio
async def test_group_cache_keeps_the_bot_that_fetched_the_quote():
    service = MicroduckQuoteService()
    service._gateway = SimpleNamespace(quote_swap=AsyncMock(return_value={"amountIn": "0.0261"}))
    common = {
        "group": "group1",
        "chain": "ethereum",
        "network": "robinhoodchain",
        "dex": "uniswap",
        "trading_type": "router",
        "side": "BUY",
        "amount": Decimal("1"),
        "max_age_seconds": 15,
    }

    first = await service.get_quote(**common, source_bot_name="bot-a")
    second = await service.get_quote(**common, source_bot_name="bot-b")

    assert first["shared_cache_hit"] is False
    assert second["shared_cache_hit"] is True
    assert second["shared_quote_group"] == "group1"
    assert second["shared_quote_source_bot_name"] == "bot-a"
    assert service._gateway.quote_swap.await_count == 1
