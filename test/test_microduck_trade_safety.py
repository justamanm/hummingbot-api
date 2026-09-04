from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("hummingbot")

from hummingbot.core.data_type.common import TradeType  # noqa: E402
from controllers.generic.microduck_profit_trailing import (  # noqa: E402
    MicroduckProfitTrailing,
    MicroduckProfitTrailingConfig,
    MicroduckTrailingRule,
    RuleState,
    is_definite_prebroadcast_failure,
)


def bare_controller() -> MicroduckProfitTrailing:
    controller = object.__new__(MicroduckProfitTrailing)
    controller.run_health = SimpleNamespace(run_id="test-run")
    controller.logger = MagicMock(return_value=MagicMock())
    controller._save_state = MagicMock()
    controller.pending_executor_id = None
    controller.pending_side = None
    controller.balance_before_base = Decimal("0")
    controller.balance_before_quote = Decimal("0")
    controller.position_base = Decimal("78")
    controller.external_balance_change = None
    controller.last_error = None
    controller.last_wallet_base_balance = None
    controller.last_wallet_quote_balance = None
    controller._last_managed_balance_check_timestamp = 0.0
    controller.config = SimpleNamespace(
        network="robinhoodchain",
        dex="uniswap",
        trading_type="router",
        wallet_address="0xabc",
        chain="ethereum",
        price_query_group=None,
    )
    return controller


@pytest.mark.asyncio
async def test_missing_microduck_balance_is_not_treated_as_zero():
    controller = bare_controller()
    controller.gateway = SimpleNamespace(
        get_balances=AsyncMock(return_value={"balances": {"USDG": "23.2"}})
    )

    with pytest.raises(ValueError, match="未返回 MICRODUCK"):
        await controller._wallet_balances()

    assert controller.last_wallet_base_balance is None


def trailing_rule(**overrides) -> MicroduckTrailingRule:
    values = {
        "buy_price_min_usd": Decimal("0.013"),
        "buy_price_upward_tolerance_usd": Decimal("0"),
        "buy_trailing_rebound_mode": "percentage",
        "buy_trailing_rebound_usd": Decimal("0.0003"),
        "buy_trailing_rebound_percent": Decimal("5"),
        "buy_trailing_rebound_adjustment_factor": Decimal("0.5"),
        "buy_trailing_rebound_max_percent": Decimal("10"),
        "sell_profit_multiple": Decimal("1.5"),
        "sell_trailing_drop_mode": "percentage",
        "sell_trailing_drop_usd": Decimal("0.0003"),
        "sell_trailing_drop_percent": Decimal("5"),
        "sell_price_downward_tolerance_usd": Decimal("0"),
        "sell_price_max_usd": Decimal("0.02"),
        "normal_check_interval": 3,
        "trailing_check_interval": 1,
    }
    values.update(overrides)
    return MicroduckTrailingRule(**values)


def test_only_definite_prebroadcast_errors_are_safe_to_retry():
    assert is_definite_prebroadcast_failure(
        ValueError("Insufficient MICRODUCK allowance to Permit2")
    )
    assert is_definite_prebroadcast_failure(
        ValueError("Insufficient funds for transaction. Please ensure you have enough ETH to cover gas costs.")
    )
    assert is_definite_prebroadcast_failure(ValueError("No routes found"))
    assert not is_definite_prebroadcast_failure(ValueError("connection reset by peer"))
    assert not is_definite_prebroadcast_failure(ValueError("Gateway timed out"))


def test_legacy_config_defaults_to_budget_mode():
    config = MicroduckProfitTrailingConfig(id="legacy")

    assert config.buy_size_mode == "budget"
    assert config.buy_budget_usd == Decimal("1")


def test_default_normal_check_interval_is_four_seconds():
    assert MicroduckProfitTrailingConfig(id="interval").normal_check_interval == 4


def test_sell_price_cap_can_be_unset_and_legacy_zero_means_unset():
    assert MicroduckProfitTrailingConfig(id="no-cap").sell_price_max_usd is None
    assert MicroduckProfitTrailingConfig(id="legacy-zero", sell_price_max_usd=Decimal("0")).sell_price_max_usd is None
    assert MicroduckProfitTrailingConfig(id="capped", sell_price_max_usd=Decimal("0.03")).sell_price_max_usd == Decimal("0.03")


def test_unset_sell_price_cap_uses_profit_target():
    rule = trailing_rule(sell_price_max_usd=None)
    rule.mark_holding(Decimal("0.02"))

    assert rule.sell_tracking_start_unit_price_usd == Decimal("0.03")


def test_sell_tolerance_applies_after_the_fixed_sell_cap():
    rule = trailing_rule(
        sell_profit_multiple=Decimal("2"),
        sell_price_max_usd=Decimal("0.038"),
        sell_price_downward_tolerance_usd=Decimal("0.001"),
    )
    rule.mark_holding(Decimal("0.021"))

    assert rule.effective_sell_target_unit_price_usd == Decimal("0.038")
    assert rule.sell_tracking_start_unit_price_usd == Decimal("0.038")
    assert rule.calculated_sell_unit_price_usd == Decimal("0.037")


def test_tolerances_only_apply_to_final_trade_limits_not_tracking_entry():
    rule = trailing_rule(
        buy_price_min_usd=Decimal("0.020"),
        buy_price_upward_tolerance_usd=Decimal("0.001"),
        sell_price_max_usd=Decimal("0.030"),
        sell_price_downward_tolerance_usd=Decimal("0.001"),
    )

    rule.evaluate_buy(Decimal("0.020"))
    assert rule.state == RuleState.TRAILING_BUY
    rule.evaluate_buy(Decimal("0.0205"))
    assert rule.state == RuleState.TRAILING_BUY
    rule.evaluate_buy(Decimal("0.0211"))
    assert rule.state == RuleState.WAITING_TO_BUY

    rule.mark_holding(Decimal("0.020"))
    rule.evaluate_sell(Decimal("0.029"))
    assert rule.state == RuleState.HOLDING
    rule.evaluate_sell(Decimal("0.030"))
    assert rule.state == RuleState.TRAILING
    assert rule.calculated_sell_unit_price_usd == Decimal("0.029")


def test_completed_rule_can_start_a_clean_next_cycle():
    rule = trailing_rule()
    rule.mark_holding(Decimal("0.02"))
    rule.trough_unit_buy_price_usd = Decimal("0.018")
    rule.peak_unit_sell_price_usd = Decimal("0.03")
    rule.mark_completed()

    rule.start_next_cycle()

    assert rule.state == RuleState.WAITING_TO_BUY
    assert rule.trough_unit_buy_price_usd == Decimal("0")
    assert rule.peak_unit_sell_price_usd == Decimal("0")
    assert rule.entry_unit_price_usd == Decimal("0")


def test_buy_amount_must_be_positive():
    with pytest.raises(ValueError, match="greater than 0"):
        MicroduckProfitTrailingConfig(
            id="invalid-quantity",
            buy_size_mode="quantity",
            buy_amount_base=Decimal("0"),
        )


def test_target_buy_amount_uses_selected_mode():
    controller = bare_controller()
    controller.config.buy_size_mode = "budget"
    controller.config.buy_budget_usd = Decimal("2")
    controller.config.buy_amount_base = Decimal("75")

    assert controller._target_buy_amount(Decimal("0.02")) == Decimal("100")

    controller.config.buy_size_mode = "quantity"
    assert controller._target_buy_amount(Decimal("0.02")) == Decimal("75")


def test_quantity_mode_rejects_approximate_buy_quote():
    controller = bare_controller()
    controller.config.buy_size_mode = "quantity"

    with pytest.raises(ValueError, match="不支持精确数量买入"):
        controller._validate_buy_quote(
            Decimal("75"),
            {"amountIn": "0.0005", "maxAmountIn": "0.00051", "approximation": True},
        )


def test_exact_buy_quote_returns_maximum_spend_and_unit_price():
    controller = bare_controller()
    controller.config.buy_size_mode = "quantity"

    maximum_spend, unit_price = controller._validate_buy_quote(
        Decimal("100"),
        {"amountIn": "1.0", "maxAmountIn": "1.02", "approximation": False},
    )

    assert maximum_spend == Decimal("1.02")
    assert unit_price == Decimal("0.0102")


@pytest.mark.asyncio
async def test_buy_reference_price_uses_one_usdg_pool_quote_without_nvda_conversion():
    controller = bare_controller()
    controller.rule = SimpleNamespace(state=RuleState.WAITING_TO_BUY)
    controller.last_price_quote_completed_at = None
    controller.last_price_quote_route = None
    controller.last_buy_price_usd = None
    controller.gateway = SimpleNamespace(
        quote_swap=AsyncMock(return_value={"amountIn": "0.03", "maxAmountIn": "0.031"})
    )

    buy_price, sell_quote = await controller._refresh_latest_unit_prices()

    assert buy_price == Decimal("0.03")
    assert sell_quote is None
    assert controller.last_buy_price_usd == Decimal("0.03")
    assert controller.gateway.quote_swap.await_count == 1
    for call in controller.gateway.quote_swap.await_args_list:
        assert call.kwargs["base_asset"] == "MICRODUCK"
        assert call.kwargs["quote_asset"] == "USDG"
        assert call.kwargs["slippage_pct"] == Decimal("0")
        assert call.kwargs["amount"] == Decimal("1")
        assert call.kwargs["side"] == TradeType.BUY


@pytest.mark.asyncio
async def test_timed_reference_quote_logs_price_and_elapsed_time():
    controller = bare_controller()
    controller.gateway = SimpleNamespace(
        quote_swap=AsyncMock(return_value={"amountIn": "0.03", "routePath": "v4_pool=test"})
    )

    quote = await controller._timed_reference_quote(Decimal("1"), TradeType.BUY)

    assert quote["amountIn"] == "0.03"
    log_message = controller.logger.return_value.info.call_args.args[0]
    assert "方向=买入" in log_message
    assert "价格=0.030000美元" in log_message
    assert "耗时=" in log_message


@pytest.mark.asyncio
async def test_timed_reference_quote_logs_group_cache_price_and_source():
    controller = bare_controller()
    controller.config.price_query_group = "group1"
    controller._shared_reference_quote = AsyncMock(return_value={
        "amountIn": "0.0261",
        "shared_quote": True,
        "shared_quote_group": "group1",
        "shared_cache_hit": True,
        "shared_cache_age_seconds": 2.3,
        "shared_quote_source_bot_name": "bot-a",
    })

    await controller._timed_reference_quote(Decimal("1"), TradeType.BUY)

    log_message = controller.logger.return_value.info.call_args.args[0]
    assert "命中分组缓存" in log_message
    assert "缓存=2.3秒" in log_message
    assert "缓存价格=0.026100美元" in log_message
    assert "来源Bot=bot-a" in log_message


@pytest.mark.asyncio
async def test_timed_reference_quote_logs_timeout_reason():
    controller = bare_controller()
    controller.gateway = SimpleNamespace(quote_swap=AsyncMock(side_effect=asyncio.TimeoutError()))

    with pytest.raises(ValueError, match="报价超时.*3.8秒"):
        await controller._timed_reference_quote(Decimal("1"), TradeType.BUY)

    log_message = controller.logger.return_value.warning.call_args.args[0]
    assert "方向=买入" in log_message
    assert "超时=3.8秒" in log_message


@pytest.mark.asyncio
async def test_timed_reference_quote_throttles_success_logs():
    controller = bare_controller()
    controller._last_reference_quote_log_timestamp = float("inf")
    controller.gateway = SimpleNamespace(
        quote_swap=AsyncMock(return_value={"amountIn": "0.03"})
    )

    await controller._timed_reference_quote(Decimal("1"), TradeType.BUY)

    controller.logger.return_value.info.assert_not_called()


@pytest.mark.asyncio
async def test_sell_reference_price_uses_one_full_position_usdg_quote():
    controller = bare_controller()
    controller.rule = SimpleNamespace(state=RuleState.HOLDING)
    controller.last_price_quote_completed_at = None
    controller.last_price_quote_route = None
    controller.last_expected_sell_usd = None
    controller.last_min_sell_usd = None
    controller.last_unit_sell_price_usd = None
    controller.gateway = SimpleNamespace(
        quote_swap=AsyncMock(return_value={"amountOut": "2.9", "minAmountOut": "2.8"})
    )

    buy_price, sell_quote = await controller._refresh_latest_unit_prices()

    assert buy_price is None
    assert sell_quote == {"amountOut": "2.9", "minAmountOut": "2.8"}
    assert controller.last_unit_sell_price_usd == Decimal("2.9") / Decimal("78")
    assert controller.last_min_sell_usd == Decimal("2.9")
    assert controller.gateway.quote_swap.await_count == 1
    call = controller.gateway.quote_swap.await_args
    assert call.kwargs["amount"] == Decimal("78")
    assert call.kwargs["side"] == TradeType.SELL


@pytest.mark.asyncio
async def test_allowance_failure_returns_sell_to_holding():
    controller = bare_controller()
    controller._wallet_balances = AsyncMock(
        return_value=(Decimal("678"), Decimal("0.001"))
    )
    controller.gateway = SimpleNamespace(
        execute_swap=AsyncMock(
            side_effect=ValueError("Insufficient MICRODUCK allowance to Permit2")
        )
    )
    controller.rule = SimpleNamespace(state=RuleState.TRAILING)
    controller.rule.mark_selling = lambda: setattr(
        controller.rule, "state", RuleState.SELLING
    )
    controller.rule.reset_after_failed_order = lambda side: setattr(
        controller.rule,
        "state",
        RuleState.HOLDING if side == TradeType.SELL else RuleState.WAITING_TO_BUY,
    )

    with pytest.raises(ValueError, match="allowance"):
        await controller._submit_swap(TradeType.SELL, Decimal("78"), Decimal("1"))

    assert controller.rule.state == RuleState.HOLDING
    assert controller.pending_side is None
    assert controller.pending_executor_id is None


@pytest.mark.asyncio
async def test_insufficient_gas_failure_returns_buy_to_waiting_for_revalidation():
    controller = bare_controller()
    controller._wallet_balances = AsyncMock(
        return_value=(Decimal("0"), Decimal("1"))
    )
    controller.gateway = SimpleNamespace(
        execute_swap=AsyncMock(
            side_effect=ValueError(
                "Insufficient funds for transaction. Please ensure you have enough ETH to cover gas costs."
            )
        )
    )
    controller.rule = SimpleNamespace(state=RuleState.TRAILING_BUY)
    controller.rule.mark_buying = lambda: setattr(controller.rule, "state", RuleState.BUYING)
    controller.rule.reset_after_failed_order = lambda side: setattr(
        controller.rule,
        "state",
        RuleState.WAITING_TO_BUY if side == TradeType.BUY else RuleState.HOLDING,
    )

    with pytest.raises(ValueError, match="Insufficient funds for transaction"):
        await controller._submit_swap(TradeType.BUY, Decimal("10"), Decimal("1"))

    assert controller.rule.state == RuleState.WAITING_TO_BUY
    assert controller.pending_side is None
    assert controller.pending_executor_id is None


@pytest.mark.asyncio
async def test_submit_swap_uses_usdg_as_the_settlement_asset():
    controller = bare_controller()
    controller._wallet_balances = AsyncMock(
        return_value=(Decimal("0"), Decimal("25"))
    )
    controller.gateway = SimpleNamespace(
        execute_swap=AsyncMock(return_value={"txHash": "0xconfirmed-later"})
    )
    controller.rule = SimpleNamespace(state=RuleState.TRAILING_BUY)
    controller.rule.mark_buying = lambda: setattr(controller.rule, "state", RuleState.BUYING)
    controller.rule.mark_selling = lambda: setattr(controller.rule, "state", RuleState.SELLING)

    await controller._submit_swap(TradeType.BUY, Decimal("10"), Decimal("0.5"))

    assert controller.gateway.execute_swap.await_args.kwargs["quote_asset"] == "USDG"
    assert controller.pending_executor_id == "0xconfirmed-later"


@pytest.mark.asyncio
async def test_external_wallet_reduction_stops_managed_position():
    controller = bare_controller()
    controller._wallet_balances = AsyncMock(
        return_value=(Decimal("0.0006"), Decimal("0.007"))
    )
    controller.pending_side = TradeType.SELL
    controller.rule = SimpleNamespace(state=RuleState.SELLING)
    controller.rule.mark_external_exit = lambda: setattr(
        controller.rule, "state", RuleState.EXTERNAL_EXIT
    )

    changed = await controller._reconcile_managed_position_balance(100.0)

    assert changed is True
    assert controller.rule.state == RuleState.EXTERNAL_EXIT
    assert controller.position_base == 0
    assert controller.pending_side is None
    assert controller.external_balance_change["managed_position_base"] == "78"
    assert controller.external_balance_change["wallet_balance_base"] == "0.0006"


@pytest.mark.asyncio
async def test_pending_sell_with_hash_is_not_mistaken_for_external_exit():
    controller = bare_controller()
    controller.pending_side = TradeType.SELL
    controller.pending_executor_id = "0xsubmitted"
    controller.rule = SimpleNamespace(state=RuleState.SELLING)
    controller._wallet_balances = AsyncMock(return_value=(Decimal("0"), Decimal("0.3")))

    changed = await controller._reconcile_managed_position_balance(100.0)

    assert changed is False
    assert controller.pending_side == TradeType.SELL
    assert controller.pending_executor_id == "0xsubmitted"
    controller._wallet_balances.assert_not_awaited()


def test_percentage_buy_rebound_triggers_without_time_confirmation():
    rule = trailing_rule()
    rule.evaluate_buy(Decimal("0.012"), 1.0)

    assert rule.state == RuleState.TRAILING_BUY
    assert rule.buy_drawdown_percent == Decimal("7.692307692307692307692307692")
    assert rule.effective_buy_rebound_percent == Decimal("8.846153846153846153846153846")
    assert rule.buy_rebound_trigger_usd == Decimal("0.01306153846153846153846153846")
    assert rule.evaluate_buy(Decimal("0.0129"), 2.0).action is None


def test_percentage_buy_rebound_uses_base_percent_without_drawdown():
    rule = trailing_rule()
    rule.evaluate_buy(Decimal("0.013"), 1.0)

    assert rule.buy_drawdown_percent == Decimal("0")
    assert rule.effective_buy_rebound_percent == Decimal("5")
    assert rule.buy_rebound_trigger_usd == Decimal("0.01365")


def test_percentage_buy_rebound_is_capped_and_recomputed_for_new_trough():
    rule = trailing_rule()
    rule.evaluate_buy(Decimal("0.012"), 1.0)
    rule.evaluate_buy(Decimal("0.0104"), 2.0)

    assert rule.buy_drawdown_percent == Decimal("20.0")
    assert rule.effective_buy_rebound_percent == Decimal("10")
    assert rule.buy_rebound_trigger_usd == Decimal("0.01144")
    assert rule.evaluate_buy(Decimal("0.01143"), 3.0).action is None
    assert rule.evaluate_buy(Decimal("0.01144"), 4.0).action == "BUY"


def test_fixed_buy_rebound_ignores_dynamic_percentage_fields():
    rule = trailing_rule(buy_trailing_rebound_mode="fixed")
    rule.evaluate_buy(Decimal("0.0104"), 1.0)

    assert rule.buy_rebound_trigger_usd == Decimal("0.0107")


def test_buy_rebound_max_percent_cannot_be_lower_than_base_percent():
    with pytest.raises(ValueError, match="最大买入反弹比例不能小于基础买入反弹比例"):
        MicroduckProfitTrailingConfig(
            id="invalid-dynamic-rebound",
            buy_trailing_rebound_percent=Decimal("11"),
            buy_trailing_rebound_max_percent=Decimal("10"),
        )


def test_percentage_sell_drop_triggers_without_time_confirmation():
    rule = trailing_rule(state=RuleState.HOLDING, entry_unit_price_usd=Decimal("0.01"))
    rule.evaluate_sell(Decimal("0.02"), 1.0)
    rule.evaluate_sell(Decimal("0.024"), 2.0)

    assert rule.state == RuleState.TRAILING
    assert rule.sell_drop_trigger_usd == Decimal("0.0228")
    assert rule.evaluate_sell(Decimal("0.0229"), 3.0).action is None
    assert rule.evaluate_sell(Decimal("0.0228"), 4.0).action == "SELL"


@pytest.mark.asyncio
async def test_original_managed_balance_restores_external_exit():
    controller = bare_controller()
    controller.rule = SimpleNamespace(state=RuleState.EXTERNAL_EXIT)
    controller.external_balance_change = {
        "managed_position_base": "78",
        "wallet_balance_base": "0.0006",
    }
    controller._wallet_balances = AsyncMock(
        return_value=(Decimal("78"), Decimal("0.007"))
    )

    changed = await controller._reconcile_managed_position_balance(100.0)

    assert changed is True
    assert controller.rule.state == RuleState.HOLDING
    assert controller.position_base == Decimal("78")
    assert controller.external_balance_change["recovered_wallet_balance_base"] == "78"


@pytest.mark.asyncio
async def test_external_exit_does_not_request_unneeded_quotes():
    controller = bare_controller()
    controller.rule = SimpleNamespace(state=RuleState.EXTERNAL_EXIT)
    controller.gateway = SimpleNamespace(quote_swap=AsyncMock())

    buy_price, sell_quote = await controller._refresh_latest_unit_prices()

    assert buy_price is None
    assert sell_quote is None
    controller.gateway.quote_swap.assert_not_awaited()


def test_external_exit_does_not_repeat_normal_status_log():
    controller = bare_controller()
    controller.rule = SimpleNamespace(state=RuleState.EXTERNAL_EXIT)
    controller._last_status_log_timestamp = 0.0
    controller.config.status_log_interval_seconds = 60

    controller._log_status_if_due(120.0)

    controller.logger.return_value.info.assert_not_called()
