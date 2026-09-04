from unittest.mock import AsyncMock

import pytest

from services.gateway_wallet_service import GatewayWalletService
from services.gecko_price_source import gateway_network_to_gecko


def test_robinhood_chain_maps_to_gecko_terminal_network():
    assert gateway_network_to_gecko("robinhoodchain") == "robinhood"


@pytest.mark.asyncio
async def test_gateway_quote_uses_only_a_listed_stablecoin():
    gateway = AsyncMock()
    gateway.get_tokens.return_value = {
        "tokens": [
            {"symbol": "WETH"},
            {"symbol": "USDT"},
        ]
    }
    service = GatewayWalletService(gateway)

    assert await service._select_gateway_quote_asset("ethereum", "example") == "USDT"


@pytest.mark.asyncio
async def test_gateway_quote_uses_robinhood_usdg_when_available():
    gateway = AsyncMock()
    gateway.get_tokens.return_value = {
        "tokens": [
            {"symbol": "WETH"},
            {"symbol": "USDG"},
            {"symbol": "microduck"},
        ]
    }
    service = GatewayWalletService(gateway)

    assert await service._select_gateway_quote_asset("ethereum", "robinhoodchain") == "USDG"


@pytest.mark.asyncio
async def test_gateway_quote_is_disabled_when_network_has_no_stablecoin():
    gateway = AsyncMock()
    gateway.get_tokens.return_value = {
        "tokens": [
            {"symbol": "WETH"},
            {"symbol": "NVDA"},
            {"symbol": "microduck"},
        ]
    }
    service = GatewayWalletService(gateway)

    assert await service._select_gateway_quote_asset("ethereum", "robinhoodchain") is None
