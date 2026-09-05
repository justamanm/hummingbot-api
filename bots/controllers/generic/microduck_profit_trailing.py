import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from time import monotonic
from typing import List, Literal, Optional

import aiohttp
from pydantic import AliasChoices, Field, model_validator

from hummingbot.core.data_type.common import MarketDict, TradeType
from hummingbot.core.gateway.gateway_http_client import GatewayHttpClient
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.models.executor_actions import ExecutorAction


class RuleState(str, Enum):
    WAITING_TO_BUY = "waiting_to_buy"
    TRAILING_BUY = "trailing_buy"
    BUYING = "buying"
    HOLDING = "holding"
    TRAILING = "trailing"
    SELLING = "selling"
    COMPLETED = "completed"
    EXTERNAL_EXIT = "external_exit"


MANAGED_BALANCE_RECONCILIATION_SECONDS = 60
MANAGED_BALANCE_DUST = Decimal("0.000001")
REFERENCE_QUOTE_TIMEOUT_SECONDS = 3.8
REFERENCE_QUOTE_LOG_INTERVAL_SECONDS = 30
MAX_REFERENCE_QUOTE_AGE_SECONDS = 15
SHARED_QUOTE_API_URL = os.getenv(
    "MICRODUCK_SHARED_QUOTE_API_URL",
    "http://127.0.0.1:24872/internal/market-data/microduck-quote",
)


def is_definite_prebroadcast_failure(error: Exception) -> bool:
    """只识别能确定没有广播到链上的 Gateway 失败。"""
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "insufficient allowance",
            "allowance to permit2",
            "allowance has expired",
            "insufficient balance",
            "insufficient funds for transaction",
            "no routes found",
            "token not found",
            "quote expired",
        )
    )


@dataclass
class TrailingDecision:
    action: Optional[str]
    state: RuleState
    peak_unit_sell_price_usd: Decimal


@dataclass
class BuyDecision:
    action: Optional[str]
    state: RuleState
    trough_unit_buy_price_usd: Decimal


class RunHealth:
    """只记录当前策略进程的健康情况，不与历史日志混在一起。"""

    def __init__(self, run_id: Optional[str] = None, started_at: Optional[str] = None):
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.run_started_at = started_at or datetime.now(timezone.utc).isoformat()
        self.last_success_at: Optional[str] = None
        self.last_failure_at: Optional[str] = None
        self.last_failure: Optional[str] = None
        self.successful_checks = 0
        self.failed_checks = 0
        self.consecutive_failures = 0

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def record_success(self, at: Optional[str] = None) -> None:
        self.last_success_at = at or self._now()
        self.successful_checks += 1
        self.consecutive_failures = 0

    def record_failure(self, message: str, at: Optional[str] = None) -> None:
        self.last_failure_at = at or self._now()
        self.last_failure = message
        self.failed_checks += 1
        self.consecutive_failures += 1


class MicroduckTrailingRule:
    """与交易接口无关的规则状态机，便于单独测试。"""

    def __init__(
        self,
        buy_price_min_usd: Decimal,
        buy_price_upward_tolerance_usd: Decimal,
        buy_trailing_rebound_mode: str,
        buy_trailing_rebound_usd: Decimal,
        buy_trailing_rebound_percent: Decimal,
        buy_trailing_rebound_adjustment_factor: Decimal,
        buy_trailing_rebound_max_percent: Decimal,
        sell_profit_multiple: Decimal,
        sell_trailing_drop_mode: str,
        sell_trailing_drop_usd: Decimal,
        sell_trailing_drop_percent: Decimal,
        sell_price_downward_tolerance_usd: Decimal,
        sell_price_max_usd: Optional[Decimal],
        normal_check_interval: int,
        buy_trailing_check_interval: int,
        sell_trailing_check_interval: int,
        state: RuleState = RuleState.WAITING_TO_BUY,
        trough_unit_buy_price_usd: Decimal = Decimal("0"),
        peak_unit_sell_price_usd: Decimal = Decimal("0"),
        entry_unit_price_usd: Decimal = Decimal("0"),
    ):
        self.buy_price_min_usd = buy_price_min_usd
        self.buy_price_upward_tolerance_usd = buy_price_upward_tolerance_usd
        self.buy_trailing_rebound_mode = buy_trailing_rebound_mode
        self.buy_trailing_rebound_usd = buy_trailing_rebound_usd
        self.buy_trailing_rebound_percent = buy_trailing_rebound_percent
        self.buy_trailing_rebound_adjustment_factor = buy_trailing_rebound_adjustment_factor
        self.buy_trailing_rebound_max_percent = buy_trailing_rebound_max_percent
        self.sell_profit_multiple = sell_profit_multiple
        self.sell_trailing_drop_mode = sell_trailing_drop_mode
        self.sell_trailing_drop_usd = sell_trailing_drop_usd
        self.sell_trailing_drop_percent = sell_trailing_drop_percent
        self.sell_price_downward_tolerance_usd = sell_price_downward_tolerance_usd
        self.sell_price_max_usd = sell_price_max_usd
        self.normal_check_interval = normal_check_interval
        self.buy_trailing_check_interval = buy_trailing_check_interval
        self.sell_trailing_check_interval = sell_trailing_check_interval
        self.state = state
        self.trough_unit_buy_price_usd = trough_unit_buy_price_usd
        self.peak_unit_sell_price_usd = peak_unit_sell_price_usd
        self.entry_unit_price_usd = entry_unit_price_usd

    @property
    def buy_drawdown_percent(self) -> Decimal:
        if self.buy_price_min_usd <= 0 or self.trough_unit_buy_price_usd <= 0:
            return Decimal("0")
        return max(
            Decimal("0"),
            (self.buy_price_min_usd - self.trough_unit_buy_price_usd)
            / self.buy_price_min_usd
            * Decimal("100"),
        )

    @property
    def effective_buy_rebound_percent(self) -> Decimal:
        return min(
            self.buy_trailing_rebound_max_percent,
            self.buy_trailing_rebound_percent
            + self.buy_drawdown_percent * self.buy_trailing_rebound_adjustment_factor,
        )

    @property
    def buy_rebound_amount_usd(self) -> Decimal:
        if self.buy_trailing_rebound_mode == "percentage":
            return self.trough_unit_buy_price_usd * self.effective_buy_rebound_percent / Decimal("100")
        return self.buy_trailing_rebound_usd

    @property
    def buy_rebound_trigger_usd(self) -> Decimal:
        return self.trough_unit_buy_price_usd + self.buy_rebound_amount_usd

    @property
    def sell_drop_amount_usd(self) -> Decimal:
        if self.sell_trailing_drop_mode == "percentage":
            return self.peak_unit_sell_price_usd * self.sell_trailing_drop_percent / Decimal("100")
        return self.sell_trailing_drop_usd

    @property
    def sell_drop_trigger_usd(self) -> Decimal:
        return self.peak_unit_sell_price_usd - self.sell_drop_amount_usd

    @property
    def target_sell_unit_price_usd(self) -> Decimal:
        return self.entry_unit_price_usd * self.sell_profit_multiple

    @property
    def effective_sell_target_unit_price_usd(self) -> Decimal:
        """利润目标与固定上限取较低者，得到最终卖出目标。"""
        if self.sell_price_max_usd is not None:
            return min(self.target_sell_unit_price_usd, self.sell_price_max_usd)
        return self.target_sell_unit_price_usd

    @property
    def calculated_sell_unit_price_usd(self) -> Decimal:
        """最终卖出目标扣除向下容差后的最低可开始卖出价格。"""
        return max(
            Decimal("0"),
            self.effective_sell_target_unit_price_usd - self.sell_price_downward_tolerance_usd,
        )

    @property
    def sell_tracking_start_unit_price_usd(self) -> Decimal:
        """开始卖出追踪只使用配置所得的目标价，不提前扣除成交容差。"""
        return self.effective_sell_target_unit_price_usd

    @property
    def buy_price_upper_usd(self) -> Decimal:
        return self.buy_price_min_usd + self.buy_price_upward_tolerance_usd

    @property
    def check_interval(self) -> int:
        if self.state == RuleState.TRAILING_BUY:
            return self.buy_trailing_check_interval
        if self.state == RuleState.TRAILING:
            return self.sell_trailing_check_interval
        return self.normal_check_interval

    def evaluate_buy(
        self,
        max_buy_price_usd: Decimal,
        now_timestamp: float = 0.0,
    ) -> BuyDecision:
        if self.state == RuleState.WAITING_TO_BUY:
            # 进入跟踪以配置买入价为准；向上容差只用于最终交易报价校验。
            if max_buy_price_usd <= self.buy_price_min_usd:
                self.state = RuleState.TRAILING_BUY
                self.trough_unit_buy_price_usd = max_buy_price_usd
            return BuyDecision(None, self.state, self.trough_unit_buy_price_usd)

        if self.state != RuleState.TRAILING_BUY:
            return BuyDecision(None, self.state, self.trough_unit_buy_price_usd)

        if max_buy_price_usd < self.trough_unit_buy_price_usd:
            self.trough_unit_buy_price_usd = max_buy_price_usd
            return BuyDecision(None, self.state, self.trough_unit_buy_price_usd)

        # 进入跟踪后允许在买入容差范围内等待反弹；超出才退出跟踪。
        if max_buy_price_usd > self.buy_price_upper_usd:
            self.state = RuleState.WAITING_TO_BUY
            self.trough_unit_buy_price_usd = Decimal("0")
            return BuyDecision(None, self.state, self.trough_unit_buy_price_usd)

        if max_buy_price_usd >= self.buy_rebound_trigger_usd:
            return BuyDecision("BUY", self.state, self.trough_unit_buy_price_usd)

        return BuyDecision(None, self.state, self.trough_unit_buy_price_usd)

    def mark_buying(self) -> None:
        self.state = RuleState.BUYING

    def mark_holding(self, entry_unit_price_usd: Decimal) -> None:
        self.state = RuleState.HOLDING
        self.entry_unit_price_usd = entry_unit_price_usd
        self.trough_unit_buy_price_usd = Decimal("0")
        self.peak_unit_sell_price_usd = Decimal("0")

    def mark_selling(self) -> None:
        self.state = RuleState.SELLING

    def mark_completed(self) -> None:
        self.state = RuleState.COMPLETED

    def start_next_cycle(self) -> None:
        """清除已完成一轮的价格跟踪状态，重新等待下一次买入。"""
        self.state = RuleState.WAITING_TO_BUY
        self.trough_unit_buy_price_usd = Decimal("0")
        self.peak_unit_sell_price_usd = Decimal("0")
        self.entry_unit_price_usd = Decimal("0")

    def mark_external_exit(self) -> None:
        self.state = RuleState.EXTERNAL_EXIT

    def reset_after_failed_order(self, side: TradeType) -> None:
        self.state = RuleState.WAITING_TO_BUY if side == TradeType.BUY else RuleState.HOLDING
        if side == TradeType.BUY:
            self.trough_unit_buy_price_usd = Decimal("0")

    def evaluate_sell(
        self,
        unit_sell_price_usd: Decimal,
        now_timestamp: float = 0.0,
    ) -> TrailingDecision:
        if self.state == RuleState.HOLDING:
            # 进入跟踪以配置推导出的卖出目标价为准；向下容差只用于最终交易报价校验。
            if unit_sell_price_usd >= self.sell_tracking_start_unit_price_usd:
                self.state = RuleState.TRAILING
                self.peak_unit_sell_price_usd = unit_sell_price_usd
            return TrailingDecision(None, self.state, self.peak_unit_sell_price_usd)

        if self.state != RuleState.TRAILING:
            return TrailingDecision(None, self.state, self.peak_unit_sell_price_usd)

        if unit_sell_price_usd > self.peak_unit_sell_price_usd:
            self.peak_unit_sell_price_usd = unit_sell_price_usd
            return TrailingDecision(None, self.state, self.peak_unit_sell_price_usd)

        if unit_sell_price_usd <= self.sell_drop_trigger_usd:
            return TrailingDecision("SELL", self.state, self.peak_unit_sell_price_usd)

        return TrailingDecision(None, self.state, self.peak_unit_sell_price_usd)


class MicroduckProfitTrailingConfig(ControllerConfigBase):
    controller_type: str = "generic"
    controller_name: str = "microduck_profit_trailing"

    connector_name: str = "uniswap"
    trading_pair: str = "MICRODUCK-USDG"
    wallet_address: str = "0x1b00113245ec6f70D21DAC3a7b7483212adABF5A"
    chain: str = "ethereum"
    network: str = "robinhoodchain"
    dex: str = "uniswap"
    trading_type: str = "router"
    # 留空时独立查询；相同名称的 Bot 可复用日常参考报价。
    price_query_group: Optional[str] = Field(default=None, max_length=64, json_schema_extra={"is_updatable": True})

    buy_price_min_usd: Decimal = Field(default=Decimal("0.012"), json_schema_extra={"is_updatable": True})
    buy_price_upward_tolerance_usd: Decimal = Field(default=Decimal("0.001"), ge=Decimal("0"), json_schema_extra={"is_updatable": True})
    buy_trailing_rebound_mode: Literal["fixed", "percentage"] = Field(default="fixed", json_schema_extra={"is_updatable": True})
    buy_trailing_rebound_usd: Decimal = Field(default=Decimal("0.0001"), gt=Decimal("0"), json_schema_extra={"is_updatable": True})
    buy_trailing_rebound_percent: Decimal = Field(default=Decimal("5"), gt=Decimal("0"), lt=Decimal("100"), json_schema_extra={"is_updatable": True})
    buy_trailing_rebound_adjustment_factor: Decimal = Field(default=Decimal("0.5"), ge=Decimal("0"), json_schema_extra={"is_updatable": True})
    buy_trailing_rebound_max_percent: Decimal = Field(default=Decimal("5"), gt=Decimal("0"), lt=Decimal("100"), json_schema_extra={"is_updatable": True})
    buy_size_mode: Literal["budget", "quantity"] = Field(default="quantity", json_schema_extra={"is_updatable": True})
    buy_budget_usd: Decimal = Field(default=Decimal("1"), json_schema_extra={"is_updatable": True})
    buy_amount_base: Decimal = Field(default=Decimal("1"), gt=Decimal("0"), json_schema_extra={"is_updatable": True})
    sell_profit_multiple: Decimal = Field(
        default=Decimal("1.5"),
        gt=Decimal("1"),
        validation_alias=AliasChoices("sell_profit_multiple", "profit_multiple"),
        json_schema_extra={"is_updatable": True},
    )
    sell_trailing_drop_mode: Literal["fixed", "percentage"] = Field(default="fixed", json_schema_extra={"is_updatable": True})
    sell_trailing_drop_usd: Decimal = Field(default=Decimal("0.003"), json_schema_extra={"is_updatable": True})
    sell_trailing_drop_percent: Decimal = Field(default=Decimal("5"), gt=Decimal("0"), lt=Decimal("100"), json_schema_extra={"is_updatable": True})
    sell_price_downward_tolerance_usd: Decimal = Field(default=Decimal("0.001"), ge=Decimal("0"), json_schema_extra={"is_updatable": True})
    # 留空表示不设上限。保留对旧配置中 0 的兼容，见下方的预处理。
    sell_price_max_usd: Optional[Decimal] = Field(default=None, ge=Decimal("0"), json_schema_extra={"is_updatable": True})
    normal_check_interval: int = Field(default=4, ge=3, json_schema_extra={"is_updatable": True})
    buy_trailing_check_interval: int = Field(default=2, ge=1, json_schema_extra={"is_updatable": True})
    sell_trailing_check_interval: int = Field(default=2, ge=1, json_schema_extra={"is_updatable": True})
    status_log_interval_seconds: int = Field(
        default=60,
        ge=15,
        json_schema_extra={"is_updatable": True},
    )

    live_trading: bool = Field(default=False, json_schema_extra={"is_updatable": True})
    auto_start_next_cycle: bool = Field(default=False, json_schema_extra={"is_updatable": True})

    @model_validator(mode="before")
    @classmethod
    def discard_removed_timing_fields(cls, values):
        """兼容已部署机器人的旧配置副本，但不再暴露或使用这些字段。"""
        if isinstance(values, dict):
            values = dict(values)
            group = values.get("price_query_group")
            if group is not None:
                group = str(group).strip()
                values["price_query_group"] = group or None
            legacy_trailing_interval = values.pop("trailing_check_interval", None)
            if legacy_trailing_interval is not None:
                values.setdefault("buy_trailing_check_interval", legacy_trailing_interval)
                values.setdefault("sell_trailing_check_interval", legacy_trailing_interval)
            # 旧版本用 0 表示“不限制”；新版本用空值，避免用户误以为 0 是有效价格上限。
            sell_price_max = values.get("sell_price_max_usd")
            if sell_price_max is not None:
                try:
                    if str(sell_price_max).strip() == "" or Decimal(str(sell_price_max)) == 0:
                        values["sell_price_max_usd"] = None
                except Exception:
                    # 交给 Pydantic 报出原始无效输入，避免在兼容逻辑里掩盖错误。
                    pass
            for field_name in (
                "buy_lowest_window_seconds",
                "buy_rebound_confirmation_seconds",
                "sell_drop_confirmation_seconds",
            ):
                values.pop(field_name, None)
            for field_name in (
                "nvda_price_url", "nvda_normal_refresh_seconds",
                "nvda_trailing_refresh_seconds", "nvda_max_trade_age_seconds",
                "nvda_max_display_age_seconds", "nvda_retry_base_seconds",
                "nvda_retry_max_seconds", "nvda_price_cache_seconds",
                "max_nvda_quote_age_seconds",
            ):
                values.pop(field_name, None)
        return values

    @model_validator(mode="after")
    def validate_rule(self):
        if self.buy_price_min_usd <= 0:
            raise ValueError("买入价格必须大于0")
        if self.buy_budget_usd <= 0:
            raise ValueError("买入预算必须大于0")
        if self.buy_trailing_rebound_max_percent < self.buy_trailing_rebound_percent:
            raise ValueError("最大买入反弹比例不能小于基础买入反弹比例")
        if self.sell_trailing_drop_usd <= 0:
            raise ValueError("币价回落金额必须大于0")
        if self.buy_trailing_check_interval >= self.normal_check_interval:
            raise ValueError("买入跟踪检查间隔必须短于普通检查间隔")
        if self.sell_trailing_check_interval >= self.normal_check_interval:
            raise ValueError("卖出跟踪检查间隔必须短于普通检查间隔")
        return self

    def update_markets(self, markets: MarketDict) -> MarketDict:
        # 本策略直接通过 Gateway 请求报价和交换，不创建传统交易所连接。
        # 当前 Hummingbot 2.16 不会把 Gateway 的 DEX 注册为普通 connector；
        # 将 uniswap 放进 markets 会导致策略在启动阶段直接失败。
        return markets


class MicroduckProfitTrailing(ControllerBase):
    """
    单次交易规则：
    1. MICRODUCK 可成交买入价低于设定价加向上浮动值后，跟踪最低价；
    2. 最低价保持设定时间，且反弹连续确认后，最多投入 1 美元；
    3. 最低可成交单价达到 1.5 倍目标价减向下浮动值后进入高频跟踪；
    4. 单价从峰值回落设定金额并连续确认后卖出；
    5. 默认仅观察。live_trading=True 才会提交真实交易。
    """

    # 跟踪、最终确认和真实交易都使用同一个 MICRODUCK/USDG 主池。
    # ETH 仅保留作链上手续费，不参与交易金额和价格计算。
    requires_rate_oracle = False

    def __init__(self, config: MicroduckProfitTrailingConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self.run_health = RunHealth()
        self.gateway = GatewayHttpClient.get_instance()
        self.rule = MicroduckTrailingRule(
            buy_price_min_usd=config.buy_price_min_usd,
            buy_price_upward_tolerance_usd=config.buy_price_upward_tolerance_usd,
            buy_trailing_rebound_mode=config.buy_trailing_rebound_mode,
            buy_trailing_rebound_usd=config.buy_trailing_rebound_usd,
            buy_trailing_rebound_percent=config.buy_trailing_rebound_percent,
            buy_trailing_rebound_adjustment_factor=config.buy_trailing_rebound_adjustment_factor,
            buy_trailing_rebound_max_percent=config.buy_trailing_rebound_max_percent,
            sell_profit_multiple=config.sell_profit_multiple,
            sell_trailing_drop_mode=config.sell_trailing_drop_mode,
            sell_trailing_drop_usd=config.sell_trailing_drop_usd,
            sell_trailing_drop_percent=config.sell_trailing_drop_percent,
            sell_price_downward_tolerance_usd=config.sell_price_downward_tolerance_usd,
            sell_price_max_usd=config.sell_price_max_usd,
            normal_check_interval=config.normal_check_interval,
            buy_trailing_check_interval=config.buy_trailing_check_interval,
            sell_trailing_check_interval=config.sell_trailing_check_interval,
        )
        self.last_check_timestamp = 0.0
        self.pending_executor_id: Optional[str] = None
        self.pending_side: Optional[TradeType] = None
        self.balance_before_base = Decimal("0")
        self.balance_before_quote = Decimal("0")
        self.last_wallet_base_balance: Optional[Decimal] = None
        self.last_wallet_quote_balance: Optional[Decimal] = None
        self.position_base = Decimal("0")
        self.entry_unit_price_usd: Optional[Decimal] = None
        self.realized_pnl_quote = Decimal("0")
        self.external_balance_change: Optional[dict] = None
        self._last_managed_balance_check_timestamp = 0.0
        self.trade_history: list[dict] = []
        self.last_buy_price_usd: Optional[Decimal] = None
        self.last_expected_sell_usd: Optional[Decimal] = None
        self.last_min_sell_usd: Optional[Decimal] = None
        self.last_unit_sell_price_usd: Optional[Decimal] = None
        self._previous_buy_reference_price_usd: Optional[Decimal] = None
        self._previous_sell_reference_price_usd: Optional[Decimal] = None
        self.last_price_quote_completed_at: Optional[str] = None
        self.last_price_quote_route: Optional[str] = None
        self.last_price_query_group: Optional[str] = None
        self.last_price_quote_cache_hit = False
        self.last_price_quote_cache_age_seconds: Optional[float] = None
        self.last_price_quote_source_bot_name: Optional[str] = None
        self._startup_logged = False
        self._last_status_log_timestamp = 0.0
        self._last_reference_quote_log_timestamp = 0.0
        self.last_error: Optional[str] = None
        self._state_path = Path("data") / f"{config.id}_microduck_profit_trailing.json"
        self._load_state()

    def update_config(self, new_config: ControllerConfigBase):
        """热更新安全字段，同时同步实际执行买卖判断的 rule 对象。"""
        previous = self.config
        restart_completed_cycle = self.rule.state == RuleState.COMPLETED
        super().update_config(new_config)
        # COMPLETED 已无持仓且不会继续交易，可以保存下一次启用时使用的买入参数。
        if self.rule.state not in {RuleState.WAITING_TO_BUY, RuleState.TRAILING_BUY, RuleState.COMPLETED}:
            self.config = self.config.model_copy(update={
                name: getattr(previous, name)
                for name in ("buy_size_mode", "buy_budget_usd", "buy_amount_base")
            })
        if self.rule.state in {RuleState.BUYING, RuleState.SELLING}:
            # 正常入口会在写文件前拦截；这里保留最后一道保护，避免手工改文件
            # 在交易确认过程中改变成交规则。
            self.config = self.config.model_copy(update={
                name: getattr(previous, name)
                for name in (
                    "buy_price_min_usd", "buy_price_upward_tolerance_usd",
                    "buy_trailing_rebound_mode", "buy_trailing_rebound_usd",
                    "buy_trailing_rebound_percent", "buy_trailing_rebound_adjustment_factor",
                    "buy_trailing_rebound_max_percent", "buy_size_mode",
                    "buy_budget_usd", "buy_amount_base",
                    "sell_profit_multiple", "sell_trailing_drop_mode",
                    "sell_trailing_drop_usd", "sell_trailing_drop_percent",
                    "sell_price_downward_tolerance_usd", "sell_price_max_usd",
                    "normal_check_interval", "buy_trailing_check_interval", "sell_trailing_check_interval",
                    "price_query_group", "live_trading",
                )
            })

        rule_fields = (
            "buy_price_min_usd", "buy_price_upward_tolerance_usd",
            "buy_trailing_rebound_mode", "buy_trailing_rebound_usd",
            "buy_trailing_rebound_percent", "buy_trailing_rebound_adjustment_factor",
            "buy_trailing_rebound_max_percent", "sell_profit_multiple",
            "sell_trailing_drop_mode", "sell_trailing_drop_usd",
            "sell_trailing_drop_percent", "sell_price_downward_tolerance_usd",
            "sell_price_max_usd", "normal_check_interval",
            "buy_trailing_check_interval", "sell_trailing_check_interval",
        )
        changed = []
        for name in rule_fields:
            value = getattr(self.config, name)
            if getattr(self.rule, name) != value:
                setattr(self.rule, name, value)
                changed.append(name)
        for name in (
            "buy_size_mode", "buy_budget_usd", "buy_amount_base",
            "price_query_group", "live_trading", "auto_start_next_cycle", "status_log_interval_seconds",
        ):
            if getattr(previous, name) != getattr(self.config, name):
                changed.append(name)
        trading_fields = set(rule_fields) | {
            "buy_size_mode", "buy_budget_usd", "buy_amount_base", "live_trading",
        }
        should_start_completed_cycle = any(name in trading_fields for name in changed) or (
            "auto_start_next_cycle" in changed and self.config.auto_start_next_cycle
        )
        if restart_completed_cycle and should_start_completed_cycle:
            self._start_next_cycle_after_config_change()
        if changed:
            self.logger().info(
                f"[运行:{self.run_health.run_id}] 运行配置已应用：{', '.join(changed)}"
            )

    def _start_next_cycle_after_config_change(self) -> None:
        """已完成策略修改交易参数后，安全地开始新一轮，而不抹去历史账目。"""
        self._start_next_cycle(
            "已完成策略检测到交易参数变更，已开始新一轮，等待进入买入范围；"
            "历史交易和累计利润已保留"
        )

    def _start_next_cycle_after_sell(self) -> None:
        """卖出确认后按配置自动开始下一轮。"""
        self._start_next_cycle(
            "卖出交易已确认，已按配置自动开始下一轮，等待进入买入范围；"
            "历史交易和累计利润已保留"
        )

    def _start_next_cycle(self, message: str) -> None:
        """清除单轮状态，但保留累计账目。"""
        self.rule.start_next_cycle()
        self.position_base = Decimal("0")
        self.pending_executor_id = None
        self.pending_side = None
        self.balance_before_base = Decimal("0")
        self.balance_before_quote = Decimal("0")
        self.external_balance_change = None
        self.last_error = None
        self.last_buy_price_usd = None
        self.last_expected_sell_usd = None
        self.last_min_sell_usd = None
        self.last_unit_sell_price_usd = None
        self._previous_buy_reference_price_usd = None
        self._previous_sell_reference_price_usd = None
        self.last_price_quote_completed_at = None
        self.last_price_quote_route = None
        self.last_price_query_group = None
        self.last_price_quote_cache_hit = False
        self.last_price_quote_cache_age_seconds = None
        self.last_price_quote_source_bot_name = None
        self._last_status_log_timestamp = 0.0
        self._save_state()
        self.logger().info(f"[运行:{self.run_health.run_id}] {message}")

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            saved = json.loads(self._state_path.read_text(encoding="utf-8"))
            self.rule.state = RuleState(saved.get("state", RuleState.WAITING_TO_BUY.value))
            self.rule.trough_unit_buy_price_usd = Decimal(str(saved.get("trough_unit_buy_price_usd", "0")))
            self.rule.peak_unit_sell_price_usd = Decimal(str(saved.get("peak_unit_sell_price_usd", "0")))
            self.rule.entry_unit_price_usd = Decimal(str(saved.get("entry_unit_price_usd", "0")))
            self.position_base = Decimal(str(saved.get("position_base", "0")))
            self.pending_executor_id = saved.get("pending_executor_id")
            pending_side = saved.get("pending_side")
            self.pending_side = TradeType[pending_side] if pending_side else None
            self.balance_before_base = Decimal(str(saved.get("balance_before_base", "0")))
            self.balance_before_quote = Decimal(str(saved.get("balance_before_quote", "0")))
            self.realized_pnl_quote = Decimal(str(saved.get("realized_pnl_quote", "0")))
            external_balance_change = saved.get("external_balance_change")
            self.external_balance_change = (
                external_balance_change if isinstance(external_balance_change, dict) else None
            )
            saved_trades = saved.get("trade_history", [])
            self.trade_history = saved_trades if isinstance(saved_trades, list) else []
            if self.rule.entry_unit_price_usd > 0:
                self.entry_unit_price_usd = self.rule.entry_unit_price_usd
        except Exception as exc:
            self.last_error = f"无法读取规则状态：{exc}"
            self.rule.state = RuleState.COMPLETED

    @staticmethod
    def _state_label(state: RuleState) -> str:
        return {
            RuleState.WAITING_TO_BUY: "等待进入买入范围",
            RuleState.TRAILING_BUY: "跟踪买入最低价",
            RuleState.BUYING: "买入交易确认中",
            RuleState.HOLDING: "持仓等待卖出目标",
            RuleState.TRAILING: "跟踪卖出最高价",
            RuleState.SELLING: "卖出交易确认中",
            RuleState.COMPLETED: "本轮交易已完成",
            RuleState.EXTERNAL_EXIT: "检测到外部余额变化，自动交易已停止",
        }[state]

    def _log_startup_once(self, now: float) -> None:
        if self._startup_logged:
            return
        buy_value = (
            f"{self.config.buy_budget_usd:.4f}美元"
            if self.config.buy_size_mode == "budget"
            else f"{self.config.buy_amount_base:.6f} MICRODUCK"
        )
        self.logger().info(
            f"[运行:{self.run_health.run_id}] MICRODUCK策略已启动："
            f"模式={'真实交易' if self.config.live_trading else '仅观察'}，"
            f"恢复状态={self._state_label(self.rule.state)}，"
            f"买入方式={'按预算' if self.config.buy_size_mode == 'budget' else '按数量'}，"
            f"买入值={buy_value}"
        )
        self.logger().info(
            f"[运行:{self.run_health.run_id}] 当前交易规则："
            f"买入上限={self.rule.buy_price_upper_usd:.6f}美元，"
            f"买入反弹={self._buy_rebound_description()}，"
            f"卖出目标={self.rule.sell_profit_multiple}倍，"
            f"卖出价格上限={'不限制' if self.rule.sell_price_max_usd is None else f'{self.rule.sell_price_max_usd:.6f}美元'}，"
            f"卖出回落={self._sell_drop_description()}，"
            f"状态提示间隔={self.config.status_log_interval_seconds}秒"
        )
        self._startup_logged = True
        self._last_status_log_timestamp = now

    def _target_buy_amount(self, max_buy_price_usd: Decimal) -> Decimal:
        if self.config.buy_size_mode == "quantity":
            return self.config.buy_amount_base
        if max_buy_price_usd <= 0:
            raise ValueError("当前买入价必须大于0")
        return self.config.buy_budget_usd / max_buy_price_usd

    def _validate_buy_quote(
        self,
        amount_base: Decimal,
        quote: dict,
    ) -> tuple[Decimal, Decimal]:
        if self.config.buy_size_mode == "quantity" and quote.get("approximation") is True:
            raise ValueError("当前路由不支持精确数量买入，本轮取消")
        if amount_base <= 0:
            raise ValueError("买入数量必须大于0")
        max_amount_in = Decimal(str(quote.get("maxAmountIn", quote["amountIn"])))
        if max_amount_in <= 0:
            raise ValueError("Gateway返回的最大USDG投入无效")
        # USDG 作为稳定币结算资产；这里的投入额即策略使用的美元计价金额。
        max_spend_usd = max_amount_in
        return max_spend_usd, max_spend_usd / amount_base

    def _log_status_if_due(self, now: float, force: bool = False) -> None:
        if not force and now - self._last_status_log_timestamp < self.config.status_log_interval_seconds:
            return

        state = self.rule.state
        prefix = f"[运行:{self.run_health.run_id}] "
        current_buy_price = f"{self.last_buy_price_usd:.6f}" if self.last_buy_price_usd is not None else "暂无"
        current_sell_price = (
            f"{self.last_unit_sell_price_usd:.6f}"
            if self.last_unit_sell_price_usd is not None
            else "暂无"
        )
        if state == RuleState.WAITING_TO_BUY:
            message = (
                f"等待买入，当前实时价格 ${current_buy_price}，"
                f"进入买入价格 ≤${self.rule.buy_price_upper_usd:.6f}"
                f"（配置买入价 ${self.rule.buy_price_min_usd:.6f}，"
                f"向上容差 ${self.rule.buy_price_upward_tolerance_usd:.6f}）"
            )
        elif state == RuleState.TRAILING_BUY:
            rebound_trigger = self.rule.buy_rebound_trigger_usd
            message = (
                f"正在跟踪买入最低价；当前={current_buy_price}美元，"
                f"最低={self.rule.trough_unit_buy_price_usd:.6f}美元，"
                f"{self._buy_tracking_description()}，"
                f"反弹触发价={rebound_trigger:.6f}美元"
            )
        elif state == RuleState.HOLDING:
            message = (
                f"持仓等待卖出，当前实时价格 ${current_sell_price}，"
                f"当前持仓 {self.position_base:.6f} MICRODUCK，"
                f"买入成本 ${self.rule.entry_unit_price_usd:.6f}，"
                f"预计卖出价格 ${self.rule.sell_tracking_start_unit_price_usd:.6f} - "
                f"${self.rule.effective_sell_target_unit_price_usd:.6f}"
                f"（利润倍数 {self.rule.sell_profit_multiple}）"
            )
        elif state == RuleState.TRAILING:
            sell_trigger = self.rule.sell_drop_trigger_usd
            message = (
                f"正在跟踪卖出最高价；当前立即卖出价={current_sell_price}美元，"
                f"最高={self.rule.peak_unit_sell_price_usd:.6f}美元，"
                f"回落方式={self._sell_drop_description()}，"
                f"回落触发价={sell_trigger:.6f}美元"
            )
        elif state in {RuleState.BUYING, RuleState.SELLING}:
            message = (
                f"{self._state_label(state)}；交易哈希="
                f"{self.pending_executor_id or '尚未取得，需要人工核对后再决定是否重试'}"
            )
        elif state == RuleState.EXTERNAL_EXIT:
            return
        else:
            message = self._state_label(state)

        source = ""
        self.logger().info(prefix + message + source)
        self._last_status_log_timestamp = now

    @staticmethod
    def _price_change_text(
        current_price_usd: Decimal,
        previous_price_usd: Optional[Decimal],
    ) -> str:
        """将相对上一轮报价的变化写为简短、不可混淆的价格标签。"""
        if previous_price_usd is None or previous_price_usd <= 0:
            return f"${current_price_usd:.6f}（首次报价）"
        change_percent = (current_price_usd / previous_price_usd - Decimal("1")) * Decimal("100")
        if change_percent > 0:
            return f"${current_price_usd:.6f}（上涨 {change_percent:.2f}%）"
        if change_percent < 0:
            return f"${current_price_usd:.6f}（下跌 {abs(change_percent):.2f}%）"
        return f"${current_price_usd:.6f}（无变化）"

    def _estimated_sell_target_usd(self) -> Decimal:
        """未成交时按配置买入价预估；成交后按实际买入成本计算最终卖出目标。"""
        entry_price = (
            self.rule.entry_unit_price_usd
            if self.rule.entry_unit_price_usd > 0
            else self.rule.buy_price_min_usd
        )
        target = entry_price * self.rule.sell_profit_multiple
        if self.rule.sell_price_max_usd is not None:
            target = min(target, self.rule.sell_price_max_usd)
        return target

    def _estimated_sell_price_usd(self) -> Decimal:
        return max(
            Decimal("0"),
            self._estimated_sell_target_usd() - self.rule.sell_price_downward_tolerance_usd,
        )

    def _buy_rebound_formula(self) -> str:
        if self.rule.buy_trailing_rebound_mode == "percentage":
            return (
                f"min({self.rule.buy_trailing_rebound_percent:g}% + "
                f"{self.rule.buy_drawdown_percent:.2f}% × "
                f"{self.rule.buy_trailing_rebound_adjustment_factor:g}，"
                f"{self.rule.buy_trailing_rebound_max_percent:g}%) = "
                f"{self.rule.effective_buy_rebound_percent:.2f}%"
            )
        return f"固定反弹 ${self.rule.buy_trailing_rebound_usd:.6f}"

    def _log_buy_tracking_quote(
        self,
        current_price_usd: Decimal,
        now: float,
        entered: bool = False,
    ) -> None:
        previous_price = self._previous_buy_reference_price_usd
        current_price = self._price_change_text(current_price_usd, previous_price)
        lowest_updated = current_price_usd == self.rule.trough_unit_buy_price_usd and (
            previous_price is None or current_price_usd < previous_price
        )
        lowest_price = (
            f"最低价格 ${self.rule.trough_unit_buy_price_usd:.6f}（更新）"
            if lowest_updated
            else f"最低价格 ${self.rule.trough_unit_buy_price_usd:.6f}"
        )
        current_rebound_percent = Decimal("0")
        if self.rule.trough_unit_buy_price_usd > 0:
            current_rebound_percent = max(
                Decimal("0"),
                (current_price_usd / self.rule.trough_unit_buy_price_usd - Decimal("1")) * Decimal("100"),
            )
        current_rebound_usd = (
            self.rule.trough_unit_buy_price_usd * current_rebound_percent / Decimal("100")
        )
        expected_buy_drawdown_percent = Decimal("0")
        if self.rule.buy_price_min_usd > 0:
            expected_buy_drawdown_percent = max(
                Decimal("0"),
                (self.rule.buy_price_min_usd - self.rule.buy_rebound_trigger_usd)
                / self.rule.buy_price_min_usd * Decimal("100"),
            )
        title = "进入买入跟踪" if entered else "买入跟踪中"
        self.logger().info(
            f"[运行:{self.run_health.run_id}] {title}，{current_price}，{lowest_price}，"
            f"相对配置的买入价下跌 {self.rule.buy_drawdown_percent:.2f}%，"
            f"当前实时回弹 {current_rebound_percent:.2f}%（${current_rebound_usd:.4f}），"
            f"最大允许反弹 {self.rule.effective_buy_rebound_percent:.2f}%"
            f"（${self.rule.buy_rebound_amount_usd:.6f}），"
            f"预计买入价格 ${self.rule.buy_rebound_trigger_usd:.6f}"
            f"（{expected_buy_drawdown_percent:.2f}%）"
            f"（配置：买入价 ${self.rule.buy_price_min_usd:.6f}；"
            f"反弹计算：{self._buy_rebound_formula()}）"
        )
        self._previous_buy_reference_price_usd = current_price_usd
        self._last_status_log_timestamp = now

    def _log_sell_tracking_quote(
        self,
        current_price_usd: Decimal,
        now: float,
        entered: bool = False,
    ) -> None:
        previous_price = self._previous_sell_reference_price_usd
        current_price = self._price_change_text(current_price_usd, previous_price)
        highest_updated = current_price_usd == self.rule.peak_unit_sell_price_usd and (
            previous_price is None or current_price_usd > previous_price
        )
        highest_price = (
            f"最高价格 ${self.rule.peak_unit_sell_price_usd:.6f}（更新）"
            if highest_updated
            else f"最高价格 ${self.rule.peak_unit_sell_price_usd:.6f}"
        )
        current_drop_percent = Decimal("0")
        if self.rule.peak_unit_sell_price_usd > 0:
            current_drop_percent = max(
                Decimal("0"),
                (Decimal("1") - current_price_usd / self.rule.peak_unit_sell_price_usd) * Decimal("100"),
            )
        configured_sell_price = self._estimated_sell_price_usd()
        configured_sell_target = self._estimated_sell_target_usd()
        compared_to_sell_price = (
            (current_price_usd / configured_sell_price - Decimal("1")) * Decimal("100")
            if configured_sell_price > 0 else Decimal("0")
        )
        compared_text = (
            f"相对配置的卖出价格上涨 {compared_to_sell_price:.2f}%"
            if compared_to_sell_price >= 0
            else f"相对配置的卖出价格下跌 {abs(compared_to_sell_price):.2f}%"
        )
        if self.rule.sell_trailing_drop_mode == "percentage":
            drop_formula = (
                f"${self.rule.peak_unit_sell_price_usd:.6f} × "
                f"(1 - {self.rule.sell_trailing_drop_percent:g}%) = "
                f"${self.rule.sell_drop_trigger_usd:.6f}"
            )
        else:
            drop_formula = f"${self.rule.peak_unit_sell_price_usd:.6f} - ${self.rule.sell_trailing_drop_usd:.6f} = ${self.rule.sell_drop_trigger_usd:.6f}"
        title = "进入卖出跟踪" if entered else "卖出跟踪中"
        self.logger().info(
            f"[运行:{self.run_health.run_id}] {title}，{current_price}，{highest_price}，"
            f"{compared_text}，当前实时回落 {current_drop_percent:.2f}%，"
            f"最大允许回落 {self._sell_drop_description()}"
            f"（${self.rule.sell_drop_trigger_usd:.6f}），"
            f"预计卖出价格 ${configured_sell_price:.6f} - ${configured_sell_target:.6f}"
            f"（配置：卖出价格 ${configured_sell_price:.6f} - ${configured_sell_target:.6f}；回落计算：{drop_formula}）"
        )
        self._previous_sell_reference_price_usd = current_price_usd
        self._last_status_log_timestamp = now

    def _buy_tracking_description(self) -> str:
        if self.rule.buy_trailing_rebound_mode != "percentage":
            return f"固定反弹={self.rule.buy_trailing_rebound_usd:.6f}美元"
        return (
            f"相对跌幅={self.rule.buy_drawdown_percent:.2f}%，"
            f"基础反弹={self.rule.buy_trailing_rebound_percent:.2f}%，"
            f"实际反弹={self.rule.effective_buy_rebound_percent:.2f}%"
        )

    def _buy_rebound_description(self) -> str:
        if self.rule.buy_trailing_rebound_mode == "percentage":
            return (
                f"基础{self.rule.buy_trailing_rebound_percent}% + "
                f"跌幅×{self.rule.buy_trailing_rebound_adjustment_factor}，"
                f"最高{self.rule.buy_trailing_rebound_max_percent}%"
            )
        return f"{self.rule.buy_trailing_rebound_usd:.6f}美元"

    def _sell_drop_description(self) -> str:
        if self.rule.sell_trailing_drop_mode == "percentage":
            return f"{self.rule.sell_trailing_drop_percent}%"
        return f"{self.rule.sell_trailing_drop_usd:.6f}美元"

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "state": self.rule.state.value,
            "trough_unit_buy_price_usd": str(self.rule.trough_unit_buy_price_usd),
            "peak_unit_sell_price_usd": str(self.rule.peak_unit_sell_price_usd),
            "entry_unit_price_usd": str(self.rule.entry_unit_price_usd),
            "position_base": str(self.position_base),
            "pending_executor_id": self.pending_executor_id,
            "pending_side": self.pending_side.name if self.pending_side else None,
            "balance_before_base": str(self.balance_before_base),
            "balance_before_quote": str(self.balance_before_quote),
            "realized_pnl_quote": str(self.realized_pnl_quote),
            "external_balance_change": self.external_balance_change,
            "wallet_address": self.config.wallet_address,
            "trade_history": self.trade_history,
        }
        temporary_path = self._state_path.with_suffix(".tmp")
        temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary_path.replace(self._state_path)

    async def _quote(
        self,
        amount: Decimal,
        side: TradeType,
        slippage_pct: Optional[Decimal] = None,
    ) -> dict:
        return await self.gateway.quote_swap(
            network=self.config.network,
            base_asset="MICRODUCK",
            quote_asset="USDG",
            amount=amount,
            side=side,
            dex=self.config.dex,
            trading_type=self.config.trading_type,
            chain=self.config.chain,
            slippage_pct=slippage_pct,
        )

    async def _reference_quote(self, amount: Decimal, side: TradeType) -> dict:
        """读取 MICRODUCK/USDG 主池，用于页面显示和策略跟踪，不发送交易。"""
        return await self.gateway.quote_swap(
            network=self.config.network,
            base_asset="MICRODUCK",
            quote_asset="USDG",
            amount=amount,
            side=side,
            dex=self.config.dex,
            trading_type=self.config.trading_type,
            chain=self.config.chain,
            # 页面与跟踪应反映市场原始报价；滑点只在准备提交交易时使用。
            slippage_pct=Decimal("0"),
        )

    async def _shared_reference_quote(self, amount: Decimal, side: TradeType) -> dict:
        """向本机 API 读取同组缓存；该接口只用于日常跟踪报价。"""
        params = {
            "group": self.config.price_query_group,
            "side": side.name,
            "amount": format(amount, "f"),
            "max_age_seconds": str(self.rule.check_interval),
            "chain": self.config.chain,
            "network": self.config.network,
            "dex": self.config.dex,
            "trading_type": self.config.trading_type,
            "source_bot_name": self._bot_instance_name(),
        }
        timeout = aiohttp.ClientTimeout(total=REFERENCE_QUOTE_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(SHARED_QUOTE_API_URL, params=params) as response:
                if response.status >= 400:
                    raise ValueError(f"共享报价服务返回 {response.status}：{await response.text()}")
                payload = await response.json()
        if not isinstance(payload, dict):
            raise ValueError("共享报价服务返回格式无效")
        return payload

    @staticmethod
    def _bot_instance_name() -> str:
        """取得当前 Hummingbot 实例名称，供共享报价记录真实来源。"""
        try:
            from hummingbot.client.hummingbot_application import HummingbotApplication

            return str(HummingbotApplication.main_application().instance_id or "")
        except Exception:
            return ""

    async def _timed_reference_quote(
        self, amount: Decimal, side: TradeType, *, allow_shared: bool = True,
    ) -> dict:
        """读取一条参考报价，并记录方向、数量、单价、耗时和失败原因。"""
        direction = "买入" if side == TradeType.BUY else "卖出"
        started_at = monotonic()
        try:
            quote = await asyncio.wait_for(
                self._shared_reference_quote(amount, side)
                if allow_shared and getattr(self.config, "price_query_group", None)
                else self._reference_quote(amount, side),
                timeout=REFERENCE_QUOTE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            elapsed = monotonic() - started_at
            message = (
                f"MICRODUCK/USDG报价超时：方向={direction}；数量={amount:.6f} MICRODUCK；"
                f"超时={REFERENCE_QUOTE_TIMEOUT_SECONDS:.1f}秒；实际等待={elapsed:.3f}秒"
            )
            self.logger().warning(f"[运行:{self.run_health.run_id}] {message}")
            raise ValueError(message) from exc
        except Exception as exc:
            elapsed = monotonic() - started_at
            self.logger().warning(
                f"[运行:{self.run_health.run_id}] MICRODUCK/USDG报价失败："
                f"方向={direction}；数量={amount:.6f} MICRODUCK；耗时={elapsed:.3f}秒；"
                f"异常类型={type(exc).__name__}；原因={exc}"
            )
            raise

        elapsed = monotonic() - started_at
        amount_key = "amountIn" if side == TradeType.BUY else "amountOut"
        total = Decimal(str(quote.get(amount_key, "0")))
        unit_price = total / amount if amount > 0 else Decimal("0")
        route = str(quote.get("routePath") or "未知路由")
        shared_quote = bool(quote.get("shared_quote"))
        shared_group = str(quote.get("shared_quote_group") or "").strip() or None
        cache_hit = shared_quote and quote.get("shared_cache_hit") is True
        cache_age = quote.get("shared_cache_age_seconds") if shared_quote else None
        source_bot_name = str(quote.get("shared_quote_source_bot_name") or "").strip() or None
        self.last_price_query_group = shared_group
        self.last_price_quote_cache_hit = cache_hit
        self.last_price_quote_cache_age_seconds = float(cache_age) if cache_age is not None else None
        self.last_price_quote_source_bot_name = source_bot_name
        if shared_quote and cache_hit:
            source = (
                f"命中分组缓存：分组={shared_group or '未知'}；"
                f"缓存={self.last_price_quote_cache_age_seconds or 0:.1f}秒；"
                f"缓存价格={unit_price:.6f}美元；来源Bot={source_bot_name or '未知'}；"
            )
        elif shared_quote:
            source = (
                f"实际查询并写入分组缓存：分组={shared_group or '未知'}；"
                f"价格={unit_price:.6f}美元；来源Bot={source_bot_name or self._bot_instance_name() or '未知'}；"
            )
        else:
            source = ""
        completed_at = monotonic()
        if (
            completed_at - getattr(self, "_last_reference_quote_log_timestamp", 0.0)
            >= REFERENCE_QUOTE_LOG_INTERVAL_SECONDS
        ):
            self.logger().info(
                f"[运行:{self.run_health.run_id}] MICRODUCK/USDG报价成功："
                f"方向={direction}；数量={amount:.6f} MICRODUCK；"
                f"价格={unit_price:.6f}美元；耗时={elapsed:.3f}秒；{source}路由={route}"
            )
            self._last_reference_quote_log_timestamp = completed_at
        return quote

    @staticmethod
    def _buy_execution_slippage_pct(
        final_unit_price_usd: Decimal,
        maximum_unit_price_usd: Decimal,
    ) -> Decimal:
        if final_unit_price_usd <= 0 or final_unit_price_usd > maximum_unit_price_usd:
            raise ValueError("最终买入价超过允许上限")
        remaining_pct = (
            maximum_unit_price_usd / final_unit_price_usd - Decimal("1")
        ) * Decimal("100")
        return min(Decimal("1"), max(Decimal("0"), remaining_pct))

    @staticmethod
    def _sell_execution_slippage_pct(
        final_unit_price_usd: Decimal,
        minimum_unit_price_usd: Decimal,
    ) -> Decimal:
        if final_unit_price_usd <= 0 or final_unit_price_usd < minimum_unit_price_usd:
            raise ValueError("最终卖出价低于允许下限")
        remaining_pct = (
            Decimal("1") - minimum_unit_price_usd / final_unit_price_usd
        ) * Decimal("100")
        return min(Decimal("1"), max(Decimal("0"), remaining_pct))

    async def _wallet_balances(self) -> tuple[Decimal, Decimal]:
        response = await self.gateway.get_balances(
            chain=self.config.chain,
            network=self.config.network,
            address=self.config.wallet_address,
            token_symbols=["MICRODUCK", "USDG"],
        )
        balances = response.get("balances", {})
        if not isinstance(balances, dict):
            raise ValueError("钱包余额查询返回格式异常，本轮不更新持仓状态")

        def required_balance(symbol: str) -> Decimal:
            for key, value in balances.items():
                if str(key).upper() == symbol:
                    return Decimal(str(value))
            # 缺字段不是余额为零。若把它当作零，会把实际持仓的 Bot 误判为
            # 用户已在外部转走资产，从而停止交易。
            raise ValueError(
                f"钱包余额查询未返回 {symbol}，本轮不更新持仓状态"
            )

        base_balance = required_balance("MICRODUCK")
        quote_balance = required_balance("USDG")
        self.last_wallet_base_balance = base_balance
        self.last_wallet_quote_balance = quote_balance
        return base_balance, quote_balance

    async def _submit_swap(
        self,
        side: TradeType,
        amount_base: Decimal,
        slippage_pct: Decimal,
    ) -> None:
        base_balance, quote_balance = await self._wallet_balances()
        self.balance_before_base = base_balance
        self.balance_before_quote = quote_balance
        self.pending_side = side
        if side == TradeType.BUY:
            self.rule.mark_buying()
        else:
            self.rule.mark_selling()
        side_label = "买入" if side == TradeType.BUY else "卖出"
        self.logger().info(
            f"[运行:{self.run_health.run_id}] 正在提交{side_label}交易："
            f"数量={amount_base:.6f} MICRODUCK，"
            f"允许滑点={slippage_pct:.6f}%"
        )
        # 先保存“正在交易”，再调用 Gateway。即使进程在发送期间退出，也不会重复下单。
        self._save_state()
        try:
            result = await self.gateway.execute_swap(
                network=self.config.network,
                base_asset="MICRODUCK",
                quote_asset="USDG",
                side=side,
                amount=amount_base,
                dex=self.config.dex,
                trading_type=self.config.trading_type,
                slippage_pct=slippage_pct,
                wallet_address=self.config.wallet_address,
                chain=self.config.chain,
            )
            transaction_hash = result.get("signature") or result.get("txHash") or result.get("hash")
            if not transaction_hash:
                raise ValueError("Gateway没有返回交易哈希；为防止重复交易，策略保持暂停")
            self.pending_executor_id = transaction_hash
            self._save_state()
            self.logger().info(
                f"[运行:{self.run_health.run_id}] {side_label}交易已提交，"
                f"等待链上确认：{transaction_hash}"
            )
        except Exception as exc:
            if is_definite_prebroadcast_failure(exc):
                self.pending_executor_id = None
                self.pending_side = None
                self.rule.reset_after_failed_order(side)
                self._save_state()
                self.logger().warning(
                    f"[运行:{self.run_health.run_id}] {side_label}交易明确未广播：{exc}；"
                    f"策略已回到{self._state_label(self.rule.state)}"
                )
                raise
            # 超时或连接中断时仍无法断言链上一定未发送；
            # 保持 BUYING/SELLING，等待人工核对，避免重复交易。
            self._save_state()
            raise

    async def _reconcile_managed_position_balance(self, now: float) -> bool:
        """核对管理持仓；外部转出时停止，原数量恢复时重新管理。"""
        # 已取得交易哈希意味着资产可能已被路由合约暂时转走。
        # 此时必须以链上交易结果为准，不能把正常卖出误判为外部转出。
        if self.pending_side is not None and self.pending_executor_id:
            return False
        if self.rule.state == RuleState.EXTERNAL_EXIT:
            if (
                now - self._last_managed_balance_check_timestamp
                < MANAGED_BALANCE_RECONCILIATION_SECONDS
            ):
                return False
            self._last_managed_balance_check_timestamp = now
            wallet_base, _ = await self._wallet_balances()
            change = self.external_balance_change or {}
            managed_position = Decimal(str(change.get("managed_position_base", "0")))
            if managed_position <= 0 or wallet_base + MANAGED_BALANCE_DUST < managed_position:
                return False
            self.position_base = managed_position
            self.rule.state = RuleState.HOLDING
            change["recovered_at"] = datetime.now(timezone.utc).isoformat()
            change["recovered_wallet_balance_base"] = str(wallet_base)
            self.external_balance_change = change
            self.last_error = None
            self._save_state()
            self.logger().info(
                f"[运行:{self.run_health.run_id}] 钱包 MICRODUCK 余额已恢复到机器人"
                f"原管理数量{managed_position:.6f}，自动交易已恢复；"
                "为避免沿用过期最高价，重新从持仓等待卖出目标开始"
            )
            return True

        if self.rule.state not in {
            RuleState.HOLDING,
            RuleState.TRAILING,
            RuleState.SELLING,
        } or self.position_base <= 0:
            return False
        force_check = self.pending_side is not None and not self.pending_executor_id
        if (
            not force_check
            and now - self._last_managed_balance_check_timestamp
            < MANAGED_BALANCE_RECONCILIATION_SECONDS
        ):
            return False
        self._last_managed_balance_check_timestamp = now
        wallet_base, _ = await self._wallet_balances()
        if wallet_base + MANAGED_BALANCE_DUST >= self.position_base:
            return False

        managed_position = self.position_base
        previous_state = self.rule.state
        self.pending_executor_id = None
        self.pending_side = None
        self.position_base = Decimal("0")
        self.rule.mark_external_exit()
        self.external_balance_change = {
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "previous_state": previous_state.value,
            "managed_position_base": str(managed_position),
            "wallet_balance_base": str(wallet_base),
            "reason": "wallet_balance_below_managed_position",
        }
        self.last_error = None
        self._save_state()
        self.logger().warning(
            f"[运行:{self.run_health.run_id}] 检测到钱包 MICRODUCK 余额已从机器人"
            f"管理的{managed_position:.6f}降至{wallet_base:.6f}；"
            "可能已在外部卖出或转出，自动交易已停止，未自动计算利润"
        )
        return True

    async def _refresh_latest_unit_prices(self) -> tuple[Optional[Decimal], Optional[dict]]:
        """按当前策略状态读取一条 MICRODUCK/USDG 参考报价。

        买入阶段按 1 个 MICRODUCK 取买入单价；卖出阶段按当前全部持仓取
        卖出报价，避免先取无用的单位双向报价、再重复取实际卖出报价。
        """
        if self.rule.state in {RuleState.WAITING_TO_BUY, RuleState.TRAILING_BUY}:
            buy_quote = await self._timed_reference_quote(Decimal("1"), TradeType.BUY)
            buy_price_usd = Decimal(str(buy_quote["amountIn"]))
            if buy_price_usd <= 0:
                raise ValueError("MICRODUCK/USDG 主池返回了无效买入报价")
            self.last_buy_price_usd = buy_price_usd
            self.last_price_quote_completed_at = datetime.now(timezone.utc).isoformat()
            self.last_price_quote_route = str(buy_quote.get("routePath") or "")
            return buy_price_usd, None

        if self.rule.state in {RuleState.HOLDING, RuleState.TRAILING} and self.position_base > 0:
            sell_quote = await self._timed_reference_quote(self.position_base, TradeType.SELL)
            expected_sell_usd = Decimal(str(sell_quote["amountOut"]))
            if expected_sell_usd <= 0:
                raise ValueError("MICRODUCK/USDG 主池返回了无效卖出报价")
            self.last_expected_sell_usd = expected_sell_usd
            self.last_min_sell_usd = expected_sell_usd
            self.last_unit_sell_price_usd = expected_sell_usd / self.position_base
            self.last_price_quote_completed_at = datetime.now(timezone.utc).isoformat()
            self.last_price_quote_route = str(sell_quote.get("routePath") or "")
            return None, sell_quote

        return None, None

    async def _reconcile_pending_order(self) -> None:
        if self.pending_side is None:
            return
        if not self.pending_executor_id:
            self.last_error = "交易发送结果不明确，已停止重复下单，需要人工核对"
            return

        status = await self.gateway.get_transaction_status(
            chain=self.config.chain,
            network=self.config.network,
            transaction_hash=self.pending_executor_id,
            fail_silently=True,
        )
        tx_status = status.get("txStatus") if isinstance(status, dict) else None
        if tx_status in (None, 0):
            return
        if tx_status != 1:
            side = self.pending_side
            self.pending_executor_id = None
            self.pending_side = None
            self.rule.reset_after_failed_order(side)
            side_label = "买入" if side == TradeType.BUY else "卖出"
            self.last_error = f"{side_label}交易链上失败：{status.get('error') or '未知原因'}"
            self._save_state()
            self.logger().warning(
                f"[运行:{self.run_health.run_id}] {self.last_error}；"
                f"策略已回到{self._state_label(self.rule.state)}"
            )
            return

        base_after, quote_after = await self._wallet_balances()
        fee_raw = status.get("fee")
        # Gateway 有时不会返回已确认交易的 Gas；未知不能伪装成零。
        fee_native = Decimal(str(fee_raw)) if fee_raw is not None else None
        side = self.pending_side
        transaction_hash = self.pending_executor_id
        self.pending_executor_id = None
        self.pending_side = None

        if side == TradeType.BUY:
            acquired_base = base_after - self.balance_before_base
            spent_usdg = self.balance_before_quote - quote_after
            if acquired_base <= 0 or spent_usdg <= 0:
                self.last_error = "买入已确认，但余额差额异常；策略暂停，需要人工核对"
                self._save_state()
                self.logger().warning(
                    f"[运行:{self.run_health.run_id}] {self.last_error}"
                )
                return
            entry_price = spent_usdg / acquired_base
            self.position_base = acquired_base
            self.entry_unit_price_usd = entry_price
            self.rule.mark_holding(entry_price)
            total_usd = spent_usdg
            self.trade_history.append(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "side": "BUY",
                    "price_usd": str(entry_price),
                    "amount_base": str(acquired_base),
                    "total_usd": str(total_usd),
                    "fee_native": str(fee_native) if fee_native is not None else None,
                    "wallet_address": self.config.wallet_address,
                    "transaction_hash": transaction_hash,
                }
            )
            self.logger().info(
                f"[运行:{self.run_health.run_id}] 买入交易已确认成功："
                f"获得={acquired_base:.6f} MICRODUCK，"
                f"实际买入单价={entry_price:.6f}美元，"
                f"卖出目标价={self.rule.target_sell_unit_price_usd:.6f}美元"
            )
        else:
            sold_base = self.balance_before_base - base_after
            received_usdg = quote_after - self.balance_before_quote
            if sold_base <= 0 or received_usdg <= 0:
                self.last_error = "卖出已确认，但余额差额异常；策略暂停，需要人工核对"
                self._save_state()
                self.logger().warning(
                    f"[运行:{self.run_health.run_id}] {self.last_error}"
                )
                return
            sell_total_usd = received_usdg
            sell_unit_price_usd = sell_total_usd / sold_base
            cost_basis_usd = sold_base * self.rule.entry_unit_price_usd
            self.realized_pnl_quote += sell_total_usd - cost_basis_usd
            self.trade_history.append(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "side": "SELL",
                    "price_usd": str(sell_unit_price_usd),
                    "amount_base": str(sold_base),
                    "total_usd": str(sell_total_usd),
                    "fee_native": str(fee_native) if fee_native is not None else None,
                    "wallet_address": self.config.wallet_address,
                    "transaction_hash": transaction_hash,
                }
            )
            self.position_base = Decimal("0")
            self.rule.mark_completed()
            self.logger().info(
                f"[运行:{self.run_health.run_id}] 卖出交易已确认成功，本轮策略完成："
                f"卖出={sold_base:.6f} MICRODUCK，收到={received_usdg:.6f} USDG"
            )
            if self.config.auto_start_next_cycle:
                self._start_next_cycle_after_sell()
        self._save_state()

    async def update_processed_data(self):
        now = self.market_data_provider.time()
        if now - self.last_check_timestamp < self.rule.check_interval:
            return
        self.last_check_timestamp = now
        self._log_startup_once(now)

        try:
            # 待确认交易优先：卖出提交后钱包余额先归零是正常现象，
            # 必须先依据哈希确认，不能先把它判成外部卖出。
            if self.pending_side:
                await self._reconcile_pending_order()
            # 安全暂停后仍需定期重新读取 MICRODUCK 持仓。此前 EXTERNAL_EXIT
            # 被下面的提前返回拦住，余额已经恢复也无法回到卖出等待。
            if self.rule.state == RuleState.EXTERNAL_EXIT:
                await self._reconcile_managed_position_balance(now)
                if self.rule.state == RuleState.EXTERNAL_EXIT:
                    self.run_health.record_success()
                    self._log_status_if_due(now)
                    self.processed_data = self.get_custom_info()
                    return
            if self.pending_side or self.rule.state in {
                RuleState.BUYING,
                RuleState.SELLING,
                RuleState.COMPLETED,
            }:
                if self.last_error is None:
                    self.run_health.record_success()
                else:
                    self.run_health.record_failure(self.last_error)
                self._log_status_if_due(now)
                self.processed_data = self.get_custom_info()
                return

            await self._reconcile_managed_position_balance(now)
            if self.rule.state == RuleState.EXTERNAL_EXIT:
                self.run_health.record_success()
                self._log_status_if_due(now)
                self.processed_data = self.get_custom_info()
                return

            buy_reference_usd, sell_reference_quote = await self._refresh_latest_unit_prices()
            self.last_error = None

            if self.rule.state in {RuleState.WAITING_TO_BUY, RuleState.TRAILING_BUY}:
                if buy_reference_usd is None:
                    raise ValueError("买入阶段未获得 MICRODUCK/USDG 买入报价")
                max_buy_price_usd = buy_reference_usd

                previous_state = self.rule.state
                buy_decision = self.rule.evaluate_buy(max_buy_price_usd, now)
                self._save_state()
                if previous_state != self.rule.state:
                    if self.rule.state == RuleState.TRAILING_BUY:
                        self._log_buy_tracking_quote(max_buy_price_usd, now, entered=True)
                    elif self.rule.state == RuleState.WAITING_TO_BUY:
                        self.logger().info(
                            f"[运行:{self.run_health.run_id}] 退出买入跟踪，"
                            f"${max_buy_price_usd:.6f} 已超出进入买入范围 "
                            f"≤${self.rule.buy_price_upper_usd:.6f}，未提交买入"
                        )
                    self._last_status_log_timestamp = now
                elif self.rule.state == RuleState.TRAILING_BUY:
                    self._log_buy_tracking_quote(max_buy_price_usd, now)
                if buy_decision.action == "BUY":
                    amount_base = self._target_buy_amount(max_buy_price_usd)
                    actual_quote = await self._timed_reference_quote(
                        amount_base,
                        TradeType.BUY,
                        allow_shared=False,
                    )
                    try:
                        max_spend_usd, final_buy_unit_price_usd = self._validate_buy_quote(
                            amount_base, actual_quote,
                        )
                    except ValueError as exc:
                        if str(exc) == "当前路由不支持精确数量买入，本轮取消":
                            self.logger().warning(
                                f"[运行:{self.run_health.run_id}] {exc}；继续跟踪价格"
                            )
                            self._last_status_log_timestamp = now
                            self.processed_data = self.get_custom_info()
                            return
                        raise
                    final_buy_decision = self.rule.evaluate_buy(
                        final_buy_unit_price_usd,
                        now,
                    )
                    self.last_buy_price_usd = final_buy_unit_price_usd
                    self.last_price_quote_completed_at = datetime.now(timezone.utc).isoformat()
                    self._save_state()
                    rebound_trigger = self.rule.buy_rebound_trigger_usd
                    maximum_buy_unit_price_usd = self.rule.buy_price_upper_usd
                    if self.config.buy_size_mode == "budget":
                        maximum_buy_unit_price_usd = min(
                            maximum_buy_unit_price_usd,
                            self.config.buy_budget_usd / amount_base,
                        )
                    if final_buy_decision.action != "BUY":
                        self.logger().info(
                            f"[运行:{self.run_health.run_id}] 买入确认取消，"
                            f"确认买入价格 ${final_buy_unit_price_usd:.6f}，"
                            f"未达到最大允许反弹 ${self.rule.effective_buy_rebound_percent:.2f}%"
                            f"（${rebound_trigger:.6f}），继续跟踪"
                        )
                    elif final_buy_unit_price_usd > maximum_buy_unit_price_usd:
                        self.logger().warning(
                            f"[运行:{self.run_health.run_id}] 买入确认拒绝，"
                            f"确认买入价格 ${final_buy_unit_price_usd:.6f} 高于"
                            f"允许上限 ${maximum_buy_unit_price_usd:.6f}，继续跟踪"
                        )
                    else:
                        execution_slippage_pct = self._buy_execution_slippage_pct(
                            final_buy_unit_price_usd,
                            maximum_buy_unit_price_usd,
                        )
                        self.logger().info(
                            f"[运行:{self.run_health.run_id}] 买入确认通过，"
                            f"买入 {amount_base:.6f} MICRODUCK，"
                            f"确认买入价格 ${final_buy_unit_price_usd:.6f}，"
                            f"预计投入 ${max_spend_usd:.6f}，"
                            f"相对配置的买入价下跌 "
                            f"{max(Decimal('0'), (Decimal('1') - final_buy_unit_price_usd / self.rule.buy_price_min_usd) * Decimal('100')):.2f}%，"
                            f"买入阶段最低价格 ${self.rule.trough_unit_buy_price_usd:.6f}，准备提交交易"
                        )
                        if self.config.live_trading:
                            await self._submit_swap(
                                TradeType.BUY,
                                amount_base,
                                execution_slippage_pct,
                            )
                        else:
                            self.logger().info(
                                f"[运行:{self.run_health.run_id}] 观察模式：买入最终确认通过，"
                                f"价格={final_buy_unit_price_usd:.6f}美元，"
                                f"数量={amount_base:.6f} MICRODUCK，"
                                f"基准花费={max_spend_usd:.4f}美元"
                            )

            elif self.rule.state in {RuleState.HOLDING, RuleState.TRAILING} and self.position_base > 0:
                if sell_reference_quote is None:
                    raise ValueError("卖出阶段未获得 MICRODUCK/USDG 卖出报价")
                sell_quote = sell_reference_quote
                expected_sell_usd = Decimal(str(sell_quote["amountOut"]))
                # 参考报价为零滑点原始报价；交易前才用最终报价和滑点保护重新确认。
                min_sell_usd = expected_sell_usd
                self.last_expected_sell_usd = expected_sell_usd
                self.last_min_sell_usd = min_sell_usd
                unit_sell_price_usd = min_sell_usd / self.position_base
                self.last_unit_sell_price_usd = unit_sell_price_usd
                previous_state = self.rule.state
                decision = self.rule.evaluate_sell(unit_sell_price_usd, now)
                self._save_state()

                if previous_state != self.rule.state and self.rule.state == RuleState.TRAILING:
                    self._log_sell_tracking_quote(unit_sell_price_usd, now, entered=True)
                elif self.rule.state == RuleState.TRAILING:
                    self._log_sell_tracking_quote(unit_sell_price_usd, now)

                if decision.action == "SELL":
                    final_sell_quote = await self._timed_reference_quote(
                        self.position_base,
                        TradeType.SELL,
                        allow_shared=False,
                    )
                    final_expected_sell_usd = (
                        Decimal(str(final_sell_quote["amountOut"]))
                    )
                    final_min_sell_usd = Decimal(
                        str(
                            final_sell_quote.get(
                                "minAmountOut", final_sell_quote["amountOut"]
                            )
                        )
                    )
                    final_sell_unit_price_usd = final_min_sell_usd / self.position_base
                    final_sell_decision = self.rule.evaluate_sell(
                        final_sell_unit_price_usd,
                        now,
                    )
                    self.last_expected_sell_usd = final_expected_sell_usd
                    self.last_min_sell_usd = final_min_sell_usd
                    self.last_unit_sell_price_usd = final_sell_unit_price_usd
                    self.last_price_quote_completed_at = datetime.now(timezone.utc).isoformat()
                    self._save_state()
                    sell_trigger = self.rule.sell_drop_trigger_usd
                    minimum_sell_unit_price_usd = (
                        self.rule.calculated_sell_unit_price_usd
                    )
                    if final_sell_decision.action != "SELL":
                        self.logger().info(
                            f"[运行:{self.run_health.run_id}] 卖出确认取消，"
                            f"确认卖出价格 ${final_sell_unit_price_usd:.6f}，"
                            f"价格重新上涨，继续跟踪"
                        )
                    elif final_sell_unit_price_usd < minimum_sell_unit_price_usd:
                        self.logger().warning(
                            f"[运行:{self.run_health.run_id}] 卖出确认拒绝，"
                            f"确认卖出价格 ${final_sell_unit_price_usd:.6f} 低于"
                            f"预计卖出价格 ${minimum_sell_unit_price_usd:.6f}，继续持仓"
                        )
                    else:
                        execution_slippage_pct = self._sell_execution_slippage_pct(
                            final_sell_unit_price_usd,
                            minimum_sell_unit_price_usd,
                        )
                        self.logger().info(
                            f"[运行:{self.run_health.run_id}] 卖出确认通过，"
                            f"卖出 {self.position_base:.6f} MICRODUCK，"
                            f"确认卖出价格 ${final_sell_unit_price_usd:.6f}，"
                            f"预计收到 ${final_min_sell_usd:.6f}，"
                            f"卖出阶段最高价格 ${self.rule.peak_unit_sell_price_usd:.6f}，准备提交交易"
                        )
                        if self.config.live_trading:
                            await self._submit_swap(
                                TradeType.SELL,
                                self.position_base,
                                execution_slippage_pct,
                            )
                        else:
                            self.logger().info(
                                f"[运行:{self.run_health.run_id}] 观察模式：卖出最终确认通过，"
                                f"预计={final_expected_sell_usd:.4f}美元，"
                                f"最低可到账={final_min_sell_usd:.4f}美元"
                            )
            if self.last_error is None:
                if self.rule.state == RuleState.WAITING_TO_BUY and buy_reference_usd is not None:
                    self._previous_buy_reference_price_usd = buy_reference_usd
                elif self.rule.state == RuleState.HOLDING and self.last_unit_sell_price_usd is not None:
                    self._previous_sell_reference_price_usd = self.last_unit_sell_price_usd
                self.run_health.record_success()
                self._log_status_if_due(now)
            else:
                self.run_health.record_failure(self.last_error)
        except Exception as exc:
            self.last_error = str(exc)
            self.run_health.record_failure(self.last_error)
            self.logger().warning(
                f"[运行:{self.run_health.run_id}] MICRODUCK规则本轮跳过：{exc}"
            )

        self.processed_data = self.get_custom_info()

    def determine_executor_actions(self) -> List[ExecutorAction]:
        return []

    def _supplemental_performance(self) -> dict:
        positions = []
        unrealized_pnl = Decimal("0")
        if self.position_base > 0 and self.rule.entry_unit_price_usd > 0:
            current_price = self.last_unit_sell_price_usd or Decimal("0")
            current_value = self.last_min_sell_usd or self.position_base * current_price
            cost_basis = self.position_base * self.rule.entry_unit_price_usd
            unrealized_pnl = current_value - cost_basis if current_value > 0 else Decimal("0")
            positions.append(
                {
                    "side": "BUY",
                    "asset": "MICRODUCK",
                    "amount": float(self.position_base),
                    "entry_price": float(self.rule.entry_unit_price_usd),
                    "current_price": float(current_price),
                    "quote_value": float(current_value),
                    "unrealized_pnl_quote": float(unrealized_pnl),
                }
            )

        volume = sum(
            (Decimal(str(trade.get("total_usd", "0"))) for trade in self.trade_history),
            Decimal("0"),
        )
        invested = sum(
            (
                Decimal(str(trade.get("total_usd", "0")))
                for trade in self.trade_history
                if trade.get("side") == "BUY"
            ),
            Decimal("0"),
        )
        global_pnl = self.realized_pnl_quote + unrealized_pnl
        global_pnl_pct = global_pnl / invested if invested > 0 else Decimal("0")
        return {
            "realized_pnl_quote": float(self.realized_pnl_quote),
            "unrealized_pnl_quote": float(unrealized_pnl),
            "global_pnl_pct": float(global_pnl_pct),
            "volume_traded": float(volume),
            "positions_summary": positions,
        }

    def get_custom_info(self) -> dict:
        price_quote_age_seconds = None
        if self.last_price_quote_completed_at:
            try:
                price_quote_age_seconds = max(
                    0.0,
                    (datetime.now(timezone.utc) - datetime.fromisoformat(self.last_price_quote_completed_at)).total_seconds(),
                )
            except ValueError:
                price_quote_age_seconds = None
        current_buy_rebound_percent = Decimal("0")
        if self.rule.trough_unit_buy_price_usd > 0 and self.last_buy_price_usd is not None:
            current_buy_rebound_percent = max(
                Decimal("0"),
                (self.last_buy_price_usd / self.rule.trough_unit_buy_price_usd - Decimal("1")) * Decimal("100"),
            )
        expected_buy_drawdown_percent = Decimal("0")
        if self.rule.buy_price_min_usd > 0 and self.rule.buy_rebound_trigger_usd > 0:
            expected_buy_drawdown_percent = max(
                Decimal("0"),
                (self.rule.buy_price_min_usd - self.rule.buy_rebound_trigger_usd)
                / self.rule.buy_price_min_usd * Decimal("100"),
            )
        return {
            "reported_at": datetime.now(timezone.utc).isoformat(),
            "price_quote_completed_at": self.last_price_quote_completed_at,
            "price_quote_age_seconds": price_quote_age_seconds,
            "price_quote_is_fresh": (
                price_quote_age_seconds is not None
                and price_quote_age_seconds <= MAX_REFERENCE_QUOTE_AGE_SECONDS
            ),
            "price_quote_route": self.last_price_quote_route,
            "price_query_group": self.last_price_query_group,
            "price_quote_cache_hit": self.last_price_quote_cache_hit,
            "price_quote_cache_age_seconds": self.last_price_quote_cache_age_seconds,
            "price_quote_source_bot_name": self.last_price_quote_source_bot_name,
            "run_id": self.run_health.run_id,
            "run_started_at": self.run_health.run_started_at,
            "last_success_at": self.run_health.last_success_at,
            "last_failure_at": self.run_health.last_failure_at,
            "successful_checks": self.run_health.successful_checks,
            "failed_checks": self.run_health.failed_checks,
            "consecutive_failures": self.run_health.consecutive_failures,
            "last_run_failure": self.run_health.last_failure,
            "state": self.rule.state.value,
            "live_trading": self.config.live_trading,
            "auto_start_next_cycle": self.config.auto_start_next_cycle,
            "check_interval_seconds": self.rule.check_interval,
            "normal_check_interval": self.rule.normal_check_interval,
            "buy_trailing_check_interval": self.rule.buy_trailing_check_interval,
            "sell_trailing_check_interval": self.rule.sell_trailing_check_interval,
            "status_log_interval_seconds": self.config.status_log_interval_seconds,
            "buy_price_min_usd": str(self.rule.buy_price_min_usd),
            "buy_price_upward_tolerance_usd": str(self.rule.buy_price_upward_tolerance_usd),
            "buy_price_upper_usd": str(self.rule.buy_price_upper_usd),
            "buy_trailing_rebound_mode": self.rule.buy_trailing_rebound_mode,
            "buy_trailing_rebound_usd": str(self.rule.buy_trailing_rebound_usd),
            "buy_trailing_rebound_percent": str(self.rule.buy_trailing_rebound_percent),
            "buy_trailing_rebound_adjustment_factor": str(self.rule.buy_trailing_rebound_adjustment_factor),
            "buy_trailing_rebound_max_percent": str(self.rule.buy_trailing_rebound_max_percent),
            "buy_drawdown_percent": str(self.rule.buy_drawdown_percent),
            "effective_buy_rebound_percent": str(self.rule.effective_buy_rebound_percent),
            "buy_size_mode": self.config.buy_size_mode,
            "buy_budget_usd": str(self.config.buy_budget_usd),
            "buy_amount_base": str(self.config.buy_amount_base),
            "buy_rebound_trigger_usd": str(self.rule.buy_rebound_trigger_usd),
            "trough_unit_buy_price_usd": str(self.rule.trough_unit_buy_price_usd),
            "buy_tracking_current_rebound_percent": str(current_buy_rebound_percent),
            "buy_tracking_expected_buy_drawdown_percent": str(expected_buy_drawdown_percent),
            "sell_profit_multiple": str(self.rule.sell_profit_multiple),
            "sell_trailing_drop_mode": self.rule.sell_trailing_drop_mode,
            "sell_trailing_drop_usd": str(self.rule.sell_trailing_drop_usd),
            "sell_trailing_drop_percent": str(self.rule.sell_trailing_drop_percent),
            "sell_drop_trigger_usd": str(self.rule.sell_drop_trigger_usd),
            "sell_price_downward_tolerance_usd": str(self.rule.sell_price_downward_tolerance_usd),
            "sell_tolerance_uses_final_target": True,
            "sell_price_max_usd": None if self.rule.sell_price_max_usd is None else str(self.rule.sell_price_max_usd),
            "position_base": str(self.position_base),
            "wallet_address": self.config.wallet_address,
            "available_base_balance": (
                str(self.last_wallet_base_balance)
                if self.last_wallet_base_balance is not None
                else None
            ),
            "entry_unit_price_usd": (
                str(self.rule.entry_unit_price_usd) if self.rule.entry_unit_price_usd > 0 else None
            ),
            "sell_tracking_start_unit_price_usd": (
                str(self.rule.sell_tracking_start_unit_price_usd)
                if self.rule.entry_unit_price_usd > 0
                else None
            ),
            "calculated_sell_unit_price_usd": (
                str(self.rule.calculated_sell_unit_price_usd)
                if self.rule.entry_unit_price_usd > 0
                else None
            ),
            "effective_sell_target_unit_price_usd": (
                str(self.rule.effective_sell_target_unit_price_usd)
                if self.rule.entry_unit_price_usd > 0
                else None
            ),
            "target_sell_unit_price_usd": (
                str(self.rule.target_sell_unit_price_usd) if self.rule.entry_unit_price_usd > 0 else None
            ),
            "buy_price_usd": str(self.last_buy_price_usd) if self.last_buy_price_usd is not None else None,
            "expected_sell_usd": str(self.last_expected_sell_usd) if self.last_expected_sell_usd is not None else None,
            "min_sell_usd": str(self.last_min_sell_usd) if self.last_min_sell_usd is not None else None,
            "unit_sell_price_usd": (
                str(self.last_unit_sell_price_usd) if self.last_unit_sell_price_usd is not None else None
            ),
            "peak_unit_sell_price_usd": str(self.rule.peak_unit_sell_price_usd),
            "pending_executor_id": self.pending_executor_id,
            "external_balance_change": self.external_balance_change,
            "supplemental_performance": self._supplemental_performance(),
            "trade_history": self.trade_history,
            "last_error": self.last_error,
        }

    def to_format_status(self) -> List[str]:
        info = self.get_custom_info()
        # 仅格式化状态文本副本；API 回报和下单数据保留原始精度。
        for key, value in info.items():
            if value is not None and key.endswith("_usd"):
                info[key] = f"{Decimal(str(value)):.6f}"
        return [
            "MICRODUCK 50%收益跟踪规则",
            f"模式：{'真实交易' if self.config.live_trading else '仅观察'}",
            f"本轮运行：{info['run_id']}（{info['run_started_at']}）",
            f"本轮检查：成功{info['successful_checks']}次，失败{info['failed_checks']}次，"
            f"连续失败{info['consecutive_failures']}次",
            f"状态：{info['state']}",
            f"当前检查间隔：{info['check_interval_seconds']}秒",
            f"可成交买入价：{info['buy_price_usd'] or '暂无'}美元",
            f"买入跟踪最低价：{info['trough_unit_buy_price_usd']}美元",
            f"实际买入单价：{info['entry_unit_price_usd'] or '暂无'}美元",
            f"50%目标卖出单价：{info['target_sell_unit_price_usd'] or '暂无'}美元",
            f"开始跟踪卖出单价：{info['sell_tracking_start_unit_price_usd'] or '暂无'}美元",
            f"预计卖出金额：{info['expected_sell_usd'] or '暂无'}美元",
            f"最低可到账：{info['min_sell_usd'] or '暂无'}美元",
            f"最低可成交单价：{info['unit_sell_price_usd'] or '暂无'}美元",
            f"单价跟踪峰值：{info['peak_unit_sell_price_usd']}美元",
            f"错误：{info['last_error'] or '无'}",
        ]
