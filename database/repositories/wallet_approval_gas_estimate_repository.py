from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import WalletApprovalGasEstimate


class WalletApprovalGasEstimateRepository:
    """保存每个钱包最近一次授权预估；预估不属于实际交易总账。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def save(
        self,
        wallet_address: str,
        token: str,
        approval_amount: Any,
        action_count: int,
        fee_per_gas_gwei: Any,
        estimated_gas_eth: Any,
    ) -> None:
        normalized_wallet = wallet_address.strip().lower()
        record = (await self.session.execute(select(WalletApprovalGasEstimate).where(
            func.lower(WalletApprovalGasEstimate.wallet_address) == normalized_wallet,
        ))).scalar_one_or_none()
        values = {
            "timestamp": datetime.now(timezone.utc),
            "wallet_address": normalized_wallet,
            "token": token.upper(),
            "approval_amount": Decimal(str(approval_amount)),
            "action_count": action_count,
            "fee_per_gas_gwei": (
                Decimal(str(fee_per_gas_gwei)) if fee_per_gas_gwei is not None else None
            ),
            "estimated_gas_eth": Decimal(str(estimated_gas_eth)),
        }
        if record is None:
            self.session.add(WalletApprovalGasEstimate(**values))
            return
        for field, value in values.items():
            setattr(record, field, value)

    async def get(self, wallet_address: str) -> dict[str, Any] | None:
        record = (await self.session.execute(select(WalletApprovalGasEstimate).where(
            func.lower(WalletApprovalGasEstimate.wallet_address) == wallet_address.strip().lower(),
        ))).scalar_one_or_none()
        if record is None:
            return None
        return {
            "timestamp": record.timestamp.isoformat(),
            "wallet_address": record.wallet_address,
            "token": record.token,
            "approval_amount": float(record.approval_amount),
            "action_count": record.action_count,
            "fee_per_gas_gwei": float(record.fee_per_gas_gwei) if record.fee_per_gas_gwei is not None else None,
            "estimated_gas_eth": float(record.estimated_gas_eth),
        }
