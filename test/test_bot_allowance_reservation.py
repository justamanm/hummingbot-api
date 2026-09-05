from routers.bot_orchestration import _controller_needs_buy_reservation


def report(state: str) -> dict:
    return {"custom_info": {"state": state}}


def test_future_buy_states_keep_allowance_reserved():
    for state in ("waiting_to_buy", "tracking"):
        assert _controller_needs_buy_reservation(report(state)) is True


def test_states_without_a_pending_buy_release_allowance():
    for state in ("holding", "trailing", "selling", "completed", "external_exit"):
        assert _controller_needs_buy_reservation(report(state)) is False
