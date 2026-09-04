#!/usr/bin/env python3
"""只读测试 MICRODUCK 行情链路；绝不调用交易执行接口。"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


API = "http://127.0.0.1:24872"
GATEWAY = "http://127.0.0.1:15888"
ROBINHOOD = "https://api.robinhood.com/rhj/prices/NVDA"
CHAINLINK_RPC = "https://arb1.arbitrum.io/rpc"
CHAINLINK_FEED = "0x4881A4418b5F2460B21d6F08CD5aA0678a7f262F"


def request(name: str, url: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json", "User-Agent": "microduck-readonly-audit/1.0"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=payload, headers=headers), timeout=15) as response:
            raw = response.read().decode()
            return {
                "name": name,
                "ok": True,
                "status": response.status,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "body": json.loads(raw),
            }
    except urllib.error.HTTPError as exc:
        return {"name": name, "ok": False, "status": exc.code, "latency_ms": round((time.perf_counter() - started) * 1000, 1), "error": exc.read().decode(errors="replace")[:500]}
    except Exception as exc:
        return {"name": name, "ok": False, "status": None, "latency_ms": round((time.perf_counter() - started) * 1000, 1), "error": str(exc)}


def chainlink_body() -> list[dict[str, Any]]:
    return [
        {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": CHAINLINK_FEED, "data": "0x313ce567"}, "latest"]},
        {"jsonrpc": "2.0", "id": 2, "method": "eth_call", "params": [{"to": CHAINLINK_FEED, "data": "0xfeaf968c"}, "latest"]},
    ]


def gateway_quote_url(pair: str, side: str) -> str:
    base, quote = pair.split("-", 1)
    # 和控制器 GatewayClient.quote_swap() 一致，直接读 Gateway；不是受登录保护的 API 包装路由。
    params = urllib.parse.urlencode({
        "chainNetwork": "ethereum-robinhoodchain",
        "connector": "uniswap/router",
        "baseToken": base,
        "quoteToken": quote,
        "amount": "1",
        "side": side,
    })
    return f"{GATEWAY}/trading/swap/quote?{params}"


def gateway_balance_body() -> dict[str, Any]:
    return {
        "network": "robinhoodchain",
        "address": "0x1b00113245ec6f70D21DAC3a7b7483212adABF5A",
        "tokens": ["MICRODUCK", "ETH"],
    }


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [sample["latency_ms"] for sample in samples]
    return {
        "count": len(samples),
        "success_count": sum(sample["ok"] for sample in samples),
        "statuses": [sample["status"] for sample in samples],
        "min_latency_ms": min(latencies),
        "median_latency_ms": round(statistics.median(latencies), 1),
        "max_latency_ms": max(latencies),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--interval-seconds", type=float, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 1 or args.interval_seconds < 0:
        raise SystemExit("samples 必须大于 0，间隔不得小于 0")

    probes: list[tuple[str, str, dict[str, Any] | None]] = [
        ("本地 NVDA 聚合报价", f"{API}/internal/market-data/nvda?max_age_seconds=15", None),
        ("Robinhood NVDA 美元报价", ROBINHOOD, None),
        ("Chainlink NVDA/USD feed", CHAINLINK_RPC, chainlink_body()),
        ("Gateway MICRODUCK→ETH 卖出报价", gateway_quote_url("MICRODUCK-ETH", "SELL"), None),
        ("Gateway ETH→MICRODUCK 买入报价", gateway_quote_url("MICRODUCK-ETH", "BUY"), None),
        ("Gateway ETH→NVDA 买入报价", gateway_quote_url("ETH-NVDA", "BUY"), None),
        ("Gateway NVDA→ETH 卖出报价", gateway_quote_url("ETH-NVDA", "SELL"), None),
        ("Gateway MICRODUCK/ETH 钱包余额", f"{GATEWAY}/chains/ethereum/balances", gateway_balance_body()),
    ]
    all_results: dict[str, list[dict[str, Any]]] = {name: [] for name, _, _ in probes}
    for index in range(args.samples):
        for name, url, body in probes:
            all_results[name].append(request(name, url, body=body))
        if index + 1 < args.samples:
            time.sleep(args.interval_seconds)
    report = {
        "tested_at": datetime.now(timezone.utc).isoformat(),
        "samples": args.samples,
        "interval_seconds": args.interval_seconds,
        "read_only": True,
        "summary": {name: summarize(samples) for name, samples in all_results.items()},
        "results": all_results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
