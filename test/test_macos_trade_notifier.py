import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "macos_trade_notifier.py"
SPEC = importlib.util.spec_from_file_location("macos_trade_notifier", SCRIPT)
notifier = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(notifier)


def _trade(tx_hash="0x1", side="BUY", display_name="bot-a"):
    return {
        "bot_name": "bot_original",
        "bot_display_name": display_name,
        "side": side,
        "transaction_hash": tx_hash,
        "amount_base": 500,
        "base_token": "MICRODUCK",
        "unit_price_usd": 0.026,
        "total_quote": 13,
        "quote_token": "USDG",
    }


def test_first_poll_only_records_history(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    monkeypatch.setattr(notifier, "fetch_confirmed_trades", lambda *_: [_trade()])
    sent = []
    monkeypatch.setattr(notifier, "send_notification", lambda *message: sent.append(message))

    assert notifier.poll_once("http://api", "u", "p", state) == 0
    assert sent == []
    assert notifier.trade_key(_trade()) in notifier.load_seen(state)[1]


def test_later_confirmed_trade_notifies_once_with_alias(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    notifier.save_seen(state, {notifier.trade_key(_trade())})
    trades = [_trade("0x2", "SELL", "新的别名"), _trade()]
    monkeypatch.setattr(notifier, "fetch_confirmed_trades", lambda *_: trades)
    sent = []
    monkeypatch.setattr(notifier, "send_notification", lambda *message: sent.append(message))

    assert notifier.poll_once("http://api", "u", "p", state) == 1
    assert notifier.poll_once("http://api", "u", "p", state) == 0
    assert sent[0][0] == "新的别名 已确认卖出"
    assert "500 MICRODUCK" in sent[0][1]


def test_container_environment_is_parsed_without_writing_password(monkeypatch):
    result = type("Result", (), {"stdout": '["USERNAME=microduck", "PASSWORD=secret"]'})()
    monkeypatch.setattr(notifier.subprocess, "run", lambda *args, **kwargs: result)

    assert notifier.read_container_env("docker", "hummingbot-api") == {
        "USERNAME": "microduck",
        "PASSWORD": "secret",
    }


def test_system_notification_test_only_accepts_local_page_origins():
    assert notifier.is_allowed_test_origin("http://localhost:3000")
    assert notifier.is_allowed_test_origin("https://127.0.0.1:8443")
    assert notifier.is_allowed_test_origin(None)
    assert not notifier.is_allowed_test_origin("https://example.com")
