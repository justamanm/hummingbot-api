from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query

from database import AsyncDatabaseManager
from database.repositories.price_query_group_repository import PriceQueryGroupRepository
from deps import get_database_manager
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
    source_bot_name: str = Query(default="", max_length=255),
    query_phase: str = Query(default="normal", pattern="^(normal|buy)$"),
    db: AsyncDatabaseManager = Depends(get_database_manager),
):
    """同组 Bot 共用参考报价；接口不用于交易前的最终报价。"""
    try:
        async with db.get_session_context() as session:
            item = await PriceQueryGroupRepository(session).get(group)
            if item is not None:
                max_age_seconds = float(
                    item.buy_trailing_check_interval
                    if query_phase == "buy" else item.normal_check_interval
                )
            else:
                # 历史配置可能早于分组表；仍以分组默认值执行，不能退回 Bot 自身间隔。
                max_age_seconds = 1.0 if query_phase == "buy" else 4.0
        return await microduck_quote_service.get_quote(
            group=group, side=side, amount=amount, max_age_seconds=max_age_seconds,
            chain=chain, network=network, dex=dex, trading_type=trading_type,
            source_bot_name=source_bot_name,
            effective_interval_seconds=max_age_seconds,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
