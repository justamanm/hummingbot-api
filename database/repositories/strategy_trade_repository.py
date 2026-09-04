from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import StrategyTradeRecord


class StrategyTradeRepository:
    """保存本系统发起的 Bot 交易及钱包授权记录。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def save_confirmed(
        self,
        bot_name: str,
        controller_id: str,
        trade: dict[str, Any],
        wallet_address: str | None = None,
    ) -> None:
        tx_hash = str(trade.get("transaction_hash") or "").strip()
        record_wallet_address = str(trade.get("wallet_address") or wallet_address or "").strip()
        # 总账以钱包为唯一归属。没有可靠钱包地址的记录不能写成孤立的 Bot 账单。
        if not tx_hash or not record_wallet_address:
            return
        existing = await self.session.execute(select(StrategyTradeRecord.id).where(
            StrategyTradeRecord.bot_name == bot_name,
            StrategyTradeRecord.controller_id == controller_id,
            StrategyTradeRecord.transaction_hash == tx_hash,
        ))
        if existing.scalar_one_or_none() is not None:
            return
        timestamp = datetime.fromisoformat(str(trade["timestamp"]).replace("Z", "+00:00"))
        self.session.add(StrategyTradeRecord(
            timestamp=timestamp,
            bot_name=bot_name,
            controller_id=controller_id,
            side=str(trade["side"]).upper(),
            record_type="TRADE",
            status="CONFIRMED",
            wallet_address=record_wallet_address,
            amount_base=Decimal(str(trade["amount_base"])),
            unit_price_usd=Decimal(str(trade["price_usd"])),
            total_quote=Decimal(str(trade["total_usd"])),
            gas_fee_native=(
                Decimal(str(trade["fee_native"]))
                if trade.get("fee_native") is not None else None
            ),
            transaction_hash=tx_hash,
        ))

    async def save_pending_approval(
        self,
        bot_name: str,
        controller_id: str,
        wallet_address: str,
        amount: str,
        transaction_hash: str,
        status: str = "PENDING",
        gas_fee_native: Any = None,
    ) -> None:
        """Store an explicit approval before confirmation, without inventing Gas."""
        tx_hash = transaction_hash.strip()
        if not tx_hash:
            return
        existing = await self.session.execute(select(StrategyTradeRecord.id).where(
            StrategyTradeRecord.bot_name == bot_name,
            StrategyTradeRecord.controller_id == controller_id,
            StrategyTradeRecord.transaction_hash == tx_hash,
        ))
        if existing.scalar_one_or_none() is not None:
            return
        self.session.add(StrategyTradeRecord(
            timestamp=datetime.now().astimezone(),
            bot_name=bot_name,
            controller_id=controller_id,
            side="APPROVE",
            record_type="APPROVAL",
            status=status,
            wallet_address=wallet_address,
            base_token="USDG",
            quote_token="USDG",
            amount_base=Decimal("0"),
            unit_price_usd=Decimal("0"),
            total_quote=Decimal("0"),
            gas_fee_native=Decimal(str(gas_fee_native)) if gas_fee_native is not None else None,
            gas_token="ETH",
            approval_amount=Decimal(amount),
            transaction_hash=tx_hash,
        ))

    async def pending_approvals(self, bot_name: str, controller_id: Optional[str]) -> list[StrategyTradeRecord]:
        query = select(StrategyTradeRecord).where(
            StrategyTradeRecord.bot_name == bot_name,
            StrategyTradeRecord.record_type == "APPROVAL",
            StrategyTradeRecord.status == "PENDING",
        )
        if controller_id:
            query = query.where(StrategyTradeRecord.controller_id == controller_id)
        return list((await self.session.execute(query)).scalars())

    async def resolve_approval(self, record: StrategyTradeRecord, status: str, gas_fee_native: Any = None) -> None:
        record.status = status
        if gas_fee_native is not None:
            record.gas_fee_native = Decimal(str(gas_fee_native))

    async def list(self, bot_name: str, controller_id: Optional[str], limit: int) -> list[dict[str, Any]]:
        query = select(StrategyTradeRecord).where(StrategyTradeRecord.bot_name == bot_name)
        if controller_id:
            query = query.where(StrategyTradeRecord.controller_id == controller_id)
        rows = list((await self.session.execute(
            query.order_by(StrategyTradeRecord.timestamp.desc()).limit(limit)
        )).scalars())
        return [self._serialize(row) for row in rows]

    async def pending_approvals_for_wallet(self, wallet_address: str) -> list[StrategyTradeRecord]:
        query = select(StrategyTradeRecord).where(
            func.lower(StrategyTradeRecord.wallet_address) == wallet_address.lower(),
            StrategyTradeRecord.record_type == "APPROVAL",
            StrategyTradeRecord.status == "PENDING",
        )
        return list((await self.session.execute(query)).scalars())

    async def list_by_wallet(
        self,
        wallet_address: str,
        limit: int,
        bot_name: Optional[str] = None,
        controller_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """按钱包读取唯一总账；可按 Bot/控制器筛选得到其引用视图。"""
        query = select(StrategyTradeRecord).where(
            func.lower(StrategyTradeRecord.wallet_address) == wallet_address.lower(),
        )
        if bot_name:
            query = query.where(StrategyTradeRecord.bot_name == bot_name)
        if controller_id:
            query = query.where(StrategyTradeRecord.controller_id == controller_id)
        rows = list((await self.session.execute(
            query.order_by(StrategyTradeRecord.timestamp.desc()).limit(limit)
        )).scalars())
        return [self._serialize(row) for row in rows]

    async def confirmed_records_missing_gas(self, wallet_address: str) -> list[StrategyTradeRecord]:
        """返回已确认、但尚未保存实际 Gas 的链上记录。

        旧版 Bot 会把未取得的 Gas 写成 0；链上实际执行的交易不应把该值当作
        真实费用，因此 0 和空值都需要用交易回执补齐。
        """
        query = select(StrategyTradeRecord).where(
            func.lower(StrategyTradeRecord.wallet_address) == wallet_address.lower(),
            StrategyTradeRecord.status == "CONFIRMED",
            StrategyTradeRecord.transaction_hash.is_not(None),
            StrategyTradeRecord.transaction_hash != "",
            or_(
                StrategyTradeRecord.gas_fee_native.is_(None),
                StrategyTradeRecord.gas_fee_native <= Decimal("0"),
            ),
        )
        return list((await self.session.execute(query)).scalars())

    async def save_actual_gas(self, record: StrategyTradeRecord, gas_fee_native: Any) -> None:
        """保存链上回执给出的实际 Gas，不用预估值覆盖账单。"""
        fee = Decimal(str(gas_fee_native))
        if fee > 0:
            record.gas_fee_native = fee

    @staticmethod
    def _serialize(row: StrategyTradeRecord) -> dict[str, Any]:
        return {
            "timestamp": row.timestamp.isoformat(),
            "bot_name": row.bot_name,
            "controller_id": row.controller_id,
            "side": row.side,
            "record_type": row.record_type,
            "status": row.status,
            "wallet_address": row.wallet_address,
            "base_token": row.base_token,
            "quote_token": row.quote_token,
            "amount_base": float(row.amount_base),
            "unit_price_usd": float(row.unit_price_usd),
            "total_quote": float(row.total_quote),
            "gas_fee_native": float(row.gas_fee_native) if row.gas_fee_native is not None else None,
            "gas_token": row.gas_token,
            "approval_amount": float(row.approval_amount) if row.approval_amount is not None else None,
            "transaction_hash": row.transaction_hash,
        }
