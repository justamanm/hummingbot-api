import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

module_path = Path(__file__).parents[1] / "services" / "nvda_price_service.py"
spec = importlib.util.spec_from_file_location("nvda_price_service_under_test", module_path)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)
NvdaPriceService = module.NvdaPriceService


def robinhood_quote():
    return {
        "bid": "217.2",
        "ask": "217.4",
        "generated_at": "2026-09-02T05:00:00+00:00",
        "source": "robinhood",
    }


def test_cached_fallback_keeps_reason_and_original_quote_age():
    service = NvdaPriceService()
    service._cache = {
        "bid": "217", "ask": "217", "source": "chainlink",
        "generated_at": "2026-09-02T05:00:00+00:00",
        "fetched_at": 1788328800,
        "fallback_error": "Robinhood不可用：429",
    }
    now = module.datetime.fromisoformat("2026-09-02T06:00:00+00:00").timestamp()
    service._cache["fetched_at"] = now - 2
    result = service._response(now)
    assert result["quote_age_seconds"] == 3600
    assert result["cache_age_seconds"] == 2
    assert result["fallback_error"] == "Robinhood不可用：429"


@pytest.mark.asyncio
async def test_fetch_time_is_request_completion_time(monkeypatch):
    service = NvdaPriceService()
    clock = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])

    async def fetch(session):
        clock[0] = 1008.0
        return robinhood_quote()

    service._fetch_robinhood = fetch
    service._quote_age_seconds = lambda quote, now: 0
    service._check_chainlink = AsyncMock()
    result = await service.get_quote(5)
    assert result["fetched_at"] == 1008.0
    assert result["cache_age_seconds"] == 0


def chainlink_quote(price="217.3"):
    return {
        "price": module.Decimal(price),
        "generated_at": "2026-09-02T05:00:00+00:00",
        "fetched_at": module.time.time(),
    }


@pytest.mark.asyncio
async def test_robinhood_quote_is_shared_for_at_least_fifteen_seconds():
    service = NvdaPriceService()
    service._fetch_robinhood = AsyncMock(return_value=robinhood_quote())
    service._quote_age_seconds = lambda quote, now: 0
    service._fetch_chainlink = AsyncMock(return_value=chainlink_quote())

    first = await service.get_quote(1)
    second = await service.get_quote(1)

    assert first["source"] == "robinhood"
    assert second["quotes"][0]["bid"] == "217.2"
    service._fetch_robinhood.assert_awaited_once()
    service._fetch_chainlink.assert_awaited_once()


@pytest.mark.asyncio
async def test_robinhood_failure_keeps_existing_robinhood_cache_without_switching_source():
    service = NvdaPriceService()
    service._fetch_robinhood = AsyncMock(return_value=robinhood_quote())
    service._quote_age_seconds = lambda quote, now: 0
    service._fetch_chainlink = AsyncMock(return_value=chainlink_quote())
    await service.get_quote(15)

    service._cache["fetched_at"] -= 16
    service._fetch_robinhood = AsyncMock(side_effect=RuntimeError("limited"))
    fallback = await service.get_quote(15)

    assert fallback["source"] == "robinhood"
    assert fallback["quotes"][0]["bid"] == "217.2"
    assert "Robinhood不可用" in fallback["fallback_error"]


@pytest.mark.asyncio
async def test_robinhood_failure_without_cache_does_not_use_chainlink():
    service = NvdaPriceService()
    service._fetch_robinhood = AsyncMock(side_effect=RuntimeError("limited"))
    with pytest.raises(RuntimeError, match="Robinhood不可用"):
        await service.get_quote(15)


@pytest.mark.asyncio
async def test_robinhood_failure_returns_existing_cache_even_if_chainlink_would_differ():
    service = NvdaPriceService()
    service._cache = {**robinhood_quote(), "fetched_at": module.time.time() - 31}
    service._fetch_robinhood = AsyncMock(side_effect=RuntimeError("limited"))
    service._fetch_chainlink = AsyncMock(return_value=chainlink_quote("13.6"))

    quote = await service.get_quote(15)

    assert quote["source"] == "robinhood"
    assert quote["stale"] is False
    assert "Robinhood不可用" in quote["fallback_error"]


def test_chainlink_latest_round_data_decoder():
    words = [1, 1360000000, 100, 200, 1]
    encoded = "0x" + "".join(f"{word:064x}" for word in words)

    answer, updated_at = NvdaPriceService._decode_latest_round_data(encoded)

    assert answer == 1360000000
    assert updated_at == 200


@pytest.mark.asyncio
async def test_stale_robinhood_quote_without_cache_fallback_raises_error():
    service = NvdaPriceService()
    service._fetch_robinhood = AsyncMock(return_value=robinhood_quote())
    service._quote_age_seconds = lambda quote, now: 25
    with pytest.raises(RuntimeError, match="Robinhood不可用"):
        await service.get_quote(15)


def test_cache_is_not_reused_after_source_quote_expires():
    service = NvdaPriceService()
    now = module.time.time()
    service._cache = {
        **robinhood_quote(),
        "generated_at": module.datetime.fromtimestamp(now - 16, module.timezone.utc).isoformat(),
        "fetched_at": now - 2,
    }

    assert service._cache_is_reusable(now, 15) is False
