from routers.bot_orchestration import _controller_needs_buy_reservation


def report(state: str) -> dict:
    return {"custom_info": {"state": state}}


def test_running_trade_states_keep_allowance_reserved():
    for state in ("waiting_to_buy", "tracking", "holding", "trailing", "selling"):
        assert _controller_needs_buy_reservation(report(state)) is True


def test_ended_trade_states_release_allowance():
    for state in ("completed", "external_exit"):
        assert _controller_needs_buy_reservation(report(state)) is False
