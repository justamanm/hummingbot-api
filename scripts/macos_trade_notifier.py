#!/usr/bin/env python3
"""浏览器关闭后，在 macOS 通知中心提示已确认的 Bot 买卖。"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("microduck_trade_notifier")


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


def format_notification(trade: dict[str, Any]) -> tuple[str, str]:
    side = str(trade.get("side") or "").upper()
    side_text = "买入" if side == "BUY" else "卖出"
    bot_name = str(trade.get("bot_display_name") or trade.get("bot_name") or "Bot")
    amount = float(trade.get("amount_base") or 0)
    base_token = str(trade.get("base_token") or "")
    price = float(trade.get("unit_price_usd") or 0)
    total = float(trade.get("total_quote") or 0)
    quote_token = str(trade.get("quote_token") or "USDG")
    title = f"{bot_name} 已确认{side_text}"
    body = f"{amount:g} {base_token}，单价 ${price:.6f}，合计 {total:.6f} {quote_token}"
    return title, body


def send_notification(title: str, body: str) -> None:
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
        send_notification(*format_notification(trade))
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
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--test-notification", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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

    while True:
        try:
            poll_once(args.api_url, username, password, args.state_file)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            LOGGER.warning("读取交易记录失败，稍后重试：%s", exc)
        if args.once:
            return 0
        time.sleep(max(args.interval, 1))


if __name__ == "__main__":
    raise SystemExit(main())
