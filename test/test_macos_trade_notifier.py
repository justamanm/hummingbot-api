import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "macos_trade_notifier.py"
SPEC = importlib.util.spec_from_file_location("macos_trade_notifier", SCRIPT)
notifier = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(notifier)


def _trade(tx_hash="0x1", side="BUY", display_name="bot-a", total=13, timestamp="2026-09-05T10:00:00Z"):
    return {
        "bot_name": "bot_original",
        "bot_display_name": display_name,
        "side": side,
        "transaction_hash": tx_hash,
        "timestamp": timestamp,
        "controller_id": "microduck",
        "wallet_address": "0x1234567890abcde",
        "amount_base": 500,
        "base_token": "MICRODUCK",
        "unit_price_usd": 0.026,
        "total_quote": total,
        "gas_fee_native": 0.000026,
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
    assert sent[0][0] == "新的别名 · 卖出成功"
    assert "500 MICRODUCK" in sent[0][1]


def test_detailed_sell_notification_puts_profit_before_secondary_details():
    buy = _trade(total=12, timestamp="2026-09-05T10:00:00Z")
    sell = _trade("0x2", "SELL", total=19, timestamp="2026-09-05T11:00:00Z")

    title, body = notifier.format_notification(sell, [sell, buy], {
        "wallet_aliases": {"0x1234567890abcde": "钱包-a"},
        "eth_usd_price": 2500,
    })

    assert title == "bot-a · 卖出成功"
    assert body.splitlines() == [
        "500 MICRODUCK × $0.026000",
        "实际收到：19.000000 USDG",
        "本次利润：+7.000000 USDG（+58.33%）",
        "",
        "钱包：钱包-a（…abcde）",
        "卖出后持仓：0 MICRODUCK",
        "Gas：0.00002600 ETH（约 $0.065000）",
    ]


def test_disabled_system_notifications_are_recorded_without_sending(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    notifier.save_seen(state, {notifier.trade_key(_trade())})
    monkeypatch.setattr(notifier, "fetch_confirmed_trades", lambda *_: [_trade("0x2"), _trade()])
    monkeypatch.setattr(notifier, "load_notification_context", lambda: {"system_notifications_enabled": False})
    sent = []
    monkeypatch.setattr(notifier, "send_notification", lambda *message: sent.append(message))

    assert notifier.poll_once("http://api", "u", "p", state) == 0
    assert sent == []
    assert notifier.trade_key(_trade("0x2")) in notifier.load_seen(state)[1]


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


def test_native_notifier_is_used_when_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(notifier, "NATIVE_NOTIFIER", "/tmp/Microduck Notifications.app")
    monkeypatch.setattr(notifier.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    notifier.send_notification("标题", "内容")

    assert calls[0][0][0] == [
        "/usr/bin/open", "-W", "-n", "-a", "/tmp/Microduck Notifications.app", "--args", "标题", "内容",
    ]
    assert calls[0][1]["check"] is True
