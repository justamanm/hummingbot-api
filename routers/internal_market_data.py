from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query

from services.microduck_quote_service import microduck_quote_service
from services.nvda_price_service import nvda_price_service


router = APIRouter(prefix="/internal/market-data", tags=["Internal Market Data"])


@router.get("/nvda")
async def get_nvda_price(max_age_seconds: int = Query(default=15, ge=1, le=300)):
    try:
        return await nvda_price_service.get_quote(max_age_seconds)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/microduck-quote")
async def get_microduck_quote(
    group: str = Query(min_length=1, max_length=64),
    side: str = Query(pattern="^(BUY|SELL)$"),
    amount: Decimal = Query(gt=0),
    max_age_seconds: float = Query(default=4, ge=1, le=15),
    chain: str = Query(default="ethereum"),
    network: str = Query(default="robinhoodchain"),
    dex: str = Query(default="uniswap"),
    trading_type: str = Query(default="router"),
):
    """同组 Bot 共用参考报价；接口不用于交易前的最终报价。"""
    try:
        return await microduck_quote_service.get_quote(
            group=group, side=side, amount=amount, max_age_seconds=max_age_seconds,
            chain=chain, network=network, dex=dex, trading_type=trading_type,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
