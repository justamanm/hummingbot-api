import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import aiohttp


class NvdaPriceService:
    """所有机器人共用的 NVDA 美元报价缓存。"""

    ROBINHOOD_URL = "https://api.robinhood.com/rhj/prices/NVDA"
    ROBINHOOD_MIN_REFRESH_SECONDS = 15

    # Chainlink 官方 NVDA / USD Data Feed（Arbitrum Mainnet）。
    # Feed 和 Robinhood REST 都以“一股 NVDA 对应多少美元”计价，无需换算。
    CHAINLINK_RPC_URL = "https://arb1.arbitrum.io/rpc"
    CHAINLINK_FEED_ADDRESS = "0x4881A4418b5F2460B21d6F08CD5aA0678a7f262F"
    CHAINLINK_CHECK_SECONDS = 300
    CHAINLINK_MAX_AGE_SECONDS = 90000
    CHAINLINK_MAX_DEVIATION_RATIO = Decimal("0.10")

    MAX_STALE_SECONDS = 1800

    def __init__(self):
        self._lock = asyncio.Lock()
        self._cache: Optional[dict] = None
        self._last_chainlink: Optional[dict] = None
        self._last_chainlink_check = 0.0

    @staticmethod
    def _valid_price(value) -> Decimal:
        price = Decimal(str(value))
        if price <= 0:
            raise ValueError("报价必须大于0")
        return price

    async def _fetch_robinhood(self, session: aiohttp.ClientSession) -> dict:
        async with session.get(
            self.ROBINHOOD_URL,
            headers={"Accept": "application/json", "User-Agent": "microduck-market-data/1.0"},
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        quotes = payload.get("quotes") or []
        if not quotes:
            raise ValueError("Robinhood没有返回NVDA报价")
        quote = quotes[0]
        return {
            "bid": str(self._valid_price(quote.get("bid"))),
            "ask": str(self._valid_price(quote.get("ask"))),
            "generated_at": quote.get("generatedAt") or datetime.now(timezone.utc).isoformat(),
            "source": "robinhood",
        }

    @staticmethod
    def _decode_uint256(value: str) -> int:
        if not isinstance(value, str) or not value.startswith("0x"):
            raise ValueError("Chainlink RPC返回格式无效")
        return int(value, 16)

    @staticmethod
    def _decode_latest_round_data(value: str) -> tuple[int, int]:
        if not isinstance(value, str) or not value.startswith("0x"):
            raise ValueError("Chainlink最新价格格式无效")
        encoded = value[2:]
        if len(encoded) < 64 * 5:
            raise ValueError("Chainlink最新价格长度无效")
        words = [encoded[index:index + 64] for index in range(0, 64 * 5, 64)]
        answer = int(words[1], 16)
        if answer >= 1 << 255:
            answer -= 1 << 256
        updated_at = int(words[3], 16)
        return answer, updated_at

    async def _fetch_chainlink(self, session: aiohttp.ClientSession) -> dict:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [{"to": self.CHAINLINK_FEED_ADDRESS, "data": "0x313ce567"}, "latest"],
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "eth_call",
                "params": [{"to": self.CHAINLINK_FEED_ADDRESS, "data": "0xfeaf968c"}, "latest"],
            },
        ]
        async with session.post(self.CHAINLINK_RPC_URL, json=requests) as response:
            response.raise_for_status()
            payload = await response.json()
        if not isinstance(payload, list):
            raise ValueError("Chainlink RPC没有返回批量结果")
        results = {item.get("id"): item for item in payload}
        if results.get(1, {}).get("error") or results.get(2, {}).get("error"):
            raise ValueError("Chainlink RPC调用失败")
        decimals = self._decode_uint256(results.get(1, {}).get("result"))
        answer, updated_at = self._decode_latest_round_data(results.get(2, {}).get("result"))
        price = self._valid_price(Decimal(answer) / (Decimal(10) ** decimals))
        age = time.time() - updated_at
        if updated_at <= 0 or age < -60 or age > self.CHAINLINK_MAX_AGE_SECONDS:
            raise ValueError(f"Chainlink报价已过期：{max(0, age):.0f}秒")
        return {
            "price": price,
            "generated_at": datetime.fromtimestamp(updated_at, timezone.utc).isoformat(),
            "fetched_at": time.time(),
        }

    @classmethod
    def _validate_chainlink_price(cls, chainlink_price: Decimal, reference_price: Decimal) -> None:
        deviation = abs(chainlink_price - reference_price) / reference_price
        if deviation > cls.CHAINLINK_MAX_DEVIATION_RATIO:
            raise ValueError(
                f"Chainlink与Robinhood价差过大：{deviation * Decimal('100'):.2f}%"
            )

    async def _check_chainlink(
        self,
        session: aiohttp.ClientSession,
        robinhood_quote: dict,
        now: float,
    ) -> None:
        if now - self._last_chainlink_check < self.CHAINLINK_CHECK_SECONDS:
            return
        chainlink = await self._fetch_chainlink(session)
        robinhood_mid = (
            Decimal(robinhood_quote["bid"]) + Decimal(robinhood_quote["ask"])
        ) / Decimal("2")
        self._validate_chainlink_price(chainlink["price"], robinhood_mid)
        self._last_chainlink = chainlink
        self._last_chainlink_check = now

    async def _fetch_chainlink_quote(self, session: aiohttp.ClientSession) -> dict:
        chainlink = await self._fetch_chainlink(session)
        price = chainlink["price"]
        if self._cache and self._cache.get("source") == "robinhood":
            reference_price = (
                Decimal(self._cache["bid"]) + Decimal(self._cache["ask"])
            ) / Decimal("2")
            self._validate_chainlink_price(price, reference_price)
        self._last_chainlink = chainlink
        self._last_chainlink_check = time.time()
        return {
            "bid": str(price),
            "ask": str(price),
            "generated_at": chainlink["generated_at"],
            "source": "chainlink",
        }

    def _response(self, now: float, error: Optional[str] = None) -> dict:
        if self._cache is None:
            raise RuntimeError(error or "NVDA报价暂时不可用")
        age = max(0.0, now - self._cache["fetched_at"])
        try:
            generated = datetime.fromisoformat(self._cache["generated_at"].replace("Z", "+00:00"))
            quote_age = max(0.0, now - generated.timestamp())
        except (ValueError, TypeError, AttributeError):
            quote_age = None
        return {
            "quotes": [{
                "symbol": "NVDA",
                "bid": self._cache["bid"],
                "ask": self._cache["ask"],
                "generatedAt": self._cache["generated_at"],
                "isTradingHalt": False,
            }],
            "source": self._cache["source"],
            "cache_age_seconds": round(age, 3),
            "stale": age > 60,
            "quote_age_seconds": round(quote_age, 3) if quote_age is not None else None,
            "fetched_at": self._cache["fetched_at"],
            "fallback_error": error or self._cache.get("fallback_error"),
            "chainlink_ready": self._last_chainlink is not None,
            "chainlink_check_age_seconds": (
                round(max(0.0, now - self._last_chainlink_check), 3)
                if self._last_chainlink is not None
                else None
            ),
        }

    @staticmethod
    def _quote_age_seconds(quote: dict, now: float) -> float:
        generated = datetime.fromisoformat(str(quote["generated_at"]).replace("Z", "+00:00"))
        return max(0.0, now - generated.timestamp())

    def _cache_is_reusable(self, now: float, max_age_seconds: int) -> bool:
        if not self._cache or now - self._cache["fetched_at"] >= max_age_seconds:
            return False
        try:
            return self._quote_age_seconds(self._cache, now) <= max_age_seconds
        except (KeyError, ValueError, TypeError, AttributeError):
            return False

    async def get_quote(self, max_age_seconds: int) -> dict:
        now = time.time()
        effective_max_age = max(max_age_seconds, self.ROBINHOOD_MIN_REFRESH_SECONDS)
        if self._cache_is_reusable(now, effective_max_age):
            return self._response(now)

        async with self._lock:
            now = time.time()
            if self._cache_is_reusable(now, effective_max_age):
                return self._response(now)

            timeout = aiohttp.ClientTimeout(total=10)
            error: Optional[str] = None
            async with aiohttp.ClientSession(timeout=timeout) as session:
                try:
                    fetched = await self._fetch_robinhood(session)
                    robinhood_age = self._quote_age_seconds(fetched, time.time())
                    if robinhood_age > effective_max_age:
                        raise ValueError(
                            f"Robinhood返回旧报价：{robinhood_age:.0f}秒，"
                            f"允许最多{effective_max_age}秒"
                        )
                    try:
                        await self._check_chainlink(session, fetched, now)
                    except Exception:
                        # Robinhood仍然可用时，Chainlink检查失败不影响主报价。
                        pass
                except Exception as exc:
                    error = f"Robinhood不可用：{exc}"
                    # Chainlink 仅用于 Robinhood 正常时的交叉检查，不能替代交易报价。
                    # 它可能在美股休市时长时间不更新；切换过去会让控制器误以为有备用源，
                    # 随后再因报价过期中断页面刷新和交易判断。
                    cache_age = now - self._cache["fetched_at"] if self._cache else float("inf")
                    if self._cache and cache_age <= self.MAX_STALE_SECONDS:
                        return self._response(now, error)
                    raise RuntimeError(error) from exc

            completed_at = time.time()
            self._cache = {**fetched, "fetched_at": completed_at, "fallback_error": error}
            return self._response(completed_at, error)


nvda_price_service = NvdaPriceService()
