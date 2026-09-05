#!/usr/bin/env python3
"""浏览器关闭后，在 macOS 通知中心提示已确认的 Bot 买卖。"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("microduck_trade_notifier")
TEST_NOTIFICATION_TITLE = "测试 Bot · 买入成功"
TEST_NOTIFICATION_BODY = "\n".join((
    "500 MICRODUCK × $0.023919",
    "实际支出：11.959500 USDG",
    "",
    "钱包：钱包-a（…a5336）",
    "买入后持仓：500 MICRODUCK",
    "Gas：0.00002600 ETH（约 $0.060000）",
))
LOCAL_ORIGIN = re.compile(r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$")
NATIVE_NOTIFIER: str | None = None
NOTIFICATION_CONTEXT_PATH: Path | None = None


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def read_container_env(docker_bin: str, container_name: str) -> dict[str, str]:
    """从正在运行的 API 容器读取认证环境，不在磁盘上复制密码。"""
    result = subprocess.run(
        [docker_bin, "inspect", container_name, "--format", "{{json .Config.Env}}"],
        check=True,
        capture_output=True,
        text=True,
    )
    values: dict[str, str] = {}
    for entry in json.loads(result.stdout):
        if "=" in entry:
            key, value = entry.split("=", 1)
            values[key] = value
    return values


def fetch_confirmed_trades(api_url: str, username: str, password: str) -> list[dict[str, Any]]:
    endpoint = f"{api_url.rstrip('/')}/bot-orchestration/strategy-trades/recent-confirmed?limit=1000"
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    request = urllib.request.Request(endpoint, headers={"Authorization": f"Basic {token}"})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
    trades = payload.get("trades", []) if isinstance(payload, dict) else []
    return trades if isinstance(trades, list) else []


def trade_key(trade: dict[str, Any]) -> str:
    return "|".join((
        str(trade.get("bot_name") or ""),
        str(trade.get("side") or "").upper(),
        str(trade.get("transaction_hash") or ""),
    ))


def _number(value: Any) -> float:
    try:
        number = float(value or 0)
        return number if number == number else 0
    except (TypeError, ValueError):
        return 0


def _trade_metrics(target: dict[str, Any], trades: list[dict[str, Any]]) -> tuple[float, float | None, float | None]:
    """按一个 Bot/控制器的成交顺序计算成交后持仓和卖出利润。"""
    related = [item for item in trades if (
        item.get("bot_name") == target.get("bot_name")
        and item.get("controller_id") == target.get("controller_id")
    )]
    related.sort(key=lambda item: str(item.get("timestamp") or ""))
    position = 0.0
    cost = 0.0
    for item in related:
        amount = max(_number(item.get("amount_base")), 0)
        total = max(_number(item.get("total_quote")), 0)
        profit = None
        profit_percent = None
        if str(item.get("side") or "").upper() == "BUY":
            position += amount
            cost += total
        else:
            sold_cost = min(amount, position) * (cost / position) if position > 0 else 0
            profit = total - sold_cost if sold_cost > 0 else None
            profit_percent = profit / sold_cost * 100 if profit is not None and sold_cost > 0 else None
            position = max(position - amount, 0)
            cost = max(cost - sold_cost, 0)
        if trade_key(item) == trade_key(target):
            return position, profit, profit_percent
    return position, None, None


def load_notification_context() -> dict[str, Any]:
    if NOTIFICATION_CONTEXT_PATH is None or not NOTIFICATION_CONTEXT_PATH.exists():
        return {}
    try:
        payload = json.loads(NOTIFICATION_CONTEXT_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


def format_notification(
    trade: dict[str, Any],
    all_trades: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
) -> tuple[str, str]:
    side = str(trade.get("side") or "").upper()
    side_text = "买入" if side == "BUY" else "卖出"
    bot_name = str(trade.get("bot_display_name") or trade.get("bot_name") or "Bot")
    amount = _number(trade.get("amount_base"))
    base_token = str(trade.get("base_token") or "")
    price = _number(trade.get("unit_price_usd"))
    total = _number(trade.get("total_quote"))
    quote_token = str(trade.get("quote_token") or "USDG")
    position, profit, profit_percent = _trade_metrics(trade, all_trades or [trade])
    wallet = str(trade.get("wallet_address") or "").lower()
    aliases = (context or {}).get("wallet_aliases", {})
    alias = str(aliases.get(wallet) or "") if isinstance(aliases, dict) else ""
    wallet_text = f"{alias}（…{wallet[-5:]}）" if alias and wallet else f"…{wallet[-5:]}" if wallet else "暂未获取"
    gas = trade.get("gas_fee_native")
    gas_value = _number(gas)
    eth_usd = _number((context or {}).get("eth_usd_price"))
    gas_text = "暂未获取" if gas is None or gas_value <= 0 else f"{gas_value:.8f} ETH"
    if gas_value > 0 and eth_usd > 0:
        gas_text += f"（约 ${gas_value * eth_usd:.6f}）"
    title = f"{bot_name} · {side_text}成功"
    lines = [
        f"{amount:g} {base_token} × ${price:.6f}",
        f"实际{'支出' if side == 'BUY' else '收到'}：{total:.6f} {quote_token}",
    ]
    if side == "SELL" and profit is not None and profit_percent is not None:
        lines.append(f"本次利润：{profit:+.6f} {quote_token}（{profit_percent:+.2f}%）")
    lines.extend(["", f"钱包：{wallet_text}", f"{side_text}后持仓：{position:g} {base_token}", f"Gas：{gas_text}"])
    body = "\n".join(lines)
    return title, body


def send_notification(title: str, body: str) -> None:
    if NATIVE_NOTIFIER:
        try:
            subprocess.run(
                ["/usr/bin/open", "-W", "-n", "-a", NATIVE_NOTIFIER, "--args", title, body],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=18,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip() or str(exc)
            raise RuntimeError(detail) from exc
        return
    script = (
        "on run argv\n"
        "display notification (item 2 of argv) with title (item 1 of argv) sound name \"default\"\n"
        "end run"
    )
    subprocess.run(
        ["/usr/bin/osascript", "-e", script, title, body],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def is_allowed_test_origin(origin: str | None) -> bool:
    """浏览器只能从本机页面触发固定内容的测试通知。"""
    return origin is None or LOCAL_ORIGIN.fullmatch(origin) is not None


class TestNotificationHandler(BaseHTTPRequestHandler):
    """本机测试入口；不接收自定义通知内容。"""

    def _send_cors_headers(self, origin: str | None) -> None:
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def do_OPTIONS(self) -> None:  # noqa: N802
        origin = self.headers.get("Origin")
        if self.path not in {"/test", "/notification-context"} or not is_allowed_test_origin(origin):
            self.send_error(403)
            return
        self.send_response(204)
        self._send_cors_headers(origin)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        origin = self.headers.get("Origin")
        if self.path == "/notification-context":
            if not is_allowed_test_origin(origin) or NOTIFICATION_CONTEXT_PATH is None:
                self.send_error(403)
                return
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 16384)
                payload = json.loads(self.rfile.read(length))
                aliases = payload.get("wallet_aliases", {})
                if not isinstance(aliases, dict):
                    raise ValueError("wallet_aliases must be an object")
                eth_usd_price = _number(payload.get("eth_usd_price"))
                clean_aliases = {
                    str(address).lower(): str(alias).strip()[:80]
                    for address, alias in aliases.items()
                    if str(address).lower().startswith("0x") and str(alias).strip()
                }
                NOTIFICATION_CONTEXT_PATH.write_text(json.dumps({
                    "wallet_aliases": clean_aliases,
                    "eth_usd_price": eth_usd_price if eth_usd_price > 0 else None,
                }, ensure_ascii=False), encoding="utf-8")
                os.chmod(NOTIFICATION_CONTEXT_PATH, 0o600)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            self.send_response(204)
            self._send_cors_headers(origin)
            self.end_headers()
            return
        if self.path != "/test":
            self.send_error(404)
            return
        if not is_allowed_test_origin(origin):
            self.send_error(403)
            return
        try:
            send_notification(TEST_NOTIFICATION_TITLE, TEST_NOTIFICATION_BODY)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            LOGGER.error("测试系统通知失败：%s", exc)
            try:
                self.send_error(500, "macOS notification failed")
            except BrokenPipeError:
                pass
            return
        self.send_response(204)
        self._send_cors_headers(origin)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.debug(format, *args)


def start_test_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), TestNotificationHandler)
    threading.Thread(target=server.serve_forever, name="notification-test-server", daemon=True).start()
    LOGGER.info("系统通知测试入口已监听 127.0.0.1:%d", port)
    return server


def load_seen(path: Path) -> tuple[bool, set[str]]:
    if not path.exists():
        return False, set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return True, set(payload.get("seen", []))
    except (OSError, ValueError, TypeError):
        return False, set()


def save_seen(path: Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"seen": sorted(seen)[-5000:]}, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(path)
    os.chmod(path, 0o600)


def poll_once(
    api_url: str,
    username: str,
    password: str,
    state_path: Path,
) -> int:
    state_exists, seen = load_seen(state_path)
    trades = fetch_confirmed_trades(api_url, username, password)
    current_keys = {trade_key(trade) for trade in trades if trade_key(trade)}
    if not state_exists:
        save_seen(state_path, current_keys)
        LOGGER.info("首次运行已记录 %d 笔历史交易，不发送旧通知", len(current_keys))
        return 0

    notified = 0
    for trade in reversed(trades):
        key = trade_key(trade)
        if not key or key in seen:
            continue
        send_notification(*format_notification(trade, trades, load_notification_context()))
        seen.add(key)
        notified += 1
    save_seen(state_path, seen | current_keys)
    if notified:
        LOGGER.info("已发送 %d 条交易通知", notified)
    return notified


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Microduck macOS 后台交易通知")
    parser.add_argument("--api-url", default="http://127.0.0.1:24872")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--docker-bin")
    parser.add_argument("--api-container", default="hummingbot-api")
    parser.add_argument("--test-port", type=int, default=24873)
    parser.add_argument("--native-notifier")
    parser.add_argument("--notification-context-file", type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--test-notification", action="store_true")
    return parser.parse_args()


def main() -> int:
    global NATIVE_NOTIFIER, NOTIFICATION_CONTEXT_PATH
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    NATIVE_NOTIFIER = args.native_notifier
    NOTIFICATION_CONTEXT_PATH = args.notification_context_file
    if args.test_notification:
        send_notification("Microduck 通知已启用", "页面关闭后，已确认的买入和卖出会显示在这里。")
        return 0

    env = read_env(args.env_file)
    if args.docker_bin and not (env.get("PASSWORD") or env.get("GATEWAY_PASSPHRASE")):
        try:
            env.update(read_container_env(args.docker_bin, args.api_container))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            LOGGER.error("无法从 API 容器读取认证配置：%s", exc)
            return 2
    username = env.get("USERNAME") or "microduck"
    password = env.get("PASSWORD") or env.get("GATEWAY_PASSPHRASE") or ""
    if not password:
        LOGGER.error("环境文件中缺少 PASSWORD 或 GATEWAY_PASSPHRASE")
        return 2

    try:
        start_test_server(args.test_port)
    except OSError as exc:
        LOGGER.error("无法启动系统通知测试入口：%s", exc)
        return 2

    while True:
        try:
            poll_once(args.api_url, username, password, args.state_file)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, urllib.error.URLError) as exc:
            LOGGER.warning("读取交易记录失败，稍后重试：%s", exc)
        if args.once:
            return 0
        time.sleep(max(args.interval, 1))


if __name__ == "__main__":
    raise SystemExit(main())
