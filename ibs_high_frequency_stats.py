#!/usr/bin/env python3
"""Independent rolling statistics for high-frequency IBS/USDT traders."""

from __future__ import annotations

import html
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import requests
from web3 import Web3

from ibs_wallet_profiler import (
    CONFIRMATION_BLOCKS,
    amount,
    connect_web3,
    current_pair_meta,
    decode_trade,
    get_swap_logs,
)


STATE_FILE = Path(os.getenv("HF_STATE_FILE", "data/ibs_high_frequency_stats.json"))
WINDOW_HOURS = int(os.getenv("HF_WINDOW_HOURS", "24"))
MIN_TOTAL_TRADES = int(os.getenv("HF_MIN_TOTAL_TRADES", "10"))
MIN_SIDE_TRADES = int(os.getenv("HF_MIN_SIDE_TRADES", "2"))
MIN_ONE_WAY_TRADES = int(os.getenv("HF_MIN_ONE_WAY_TRADES", "8"))
REPORT_MINUTES = int(os.getenv("HF_REPORT_MINUTES", "60"))
TOP_LIMIT = int(os.getenv("HF_TOP_LIMIT", "8"))
BOOTSTRAP_BLOCKS = int(os.getenv("HF_BOOTSTRAP_BLOCKS", "30000"))
MAX_SCAN_BLOCKS = int(os.getenv("HF_MAX_SCAN_BLOCKS", "30000"))
TELEGRAM_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "20"))


def utc_now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"schema_version": 1, "events": [], "alerted": {}}
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    state.setdefault("events", [])
    state.setdefault("alerted", {})
    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(STATE_FILE)


def collect_events(web3: Web3, logs: Iterable[Any], ibs_is_token0: bool) -> list[dict[str, Any]]:
    tx_from: dict[str, str] = {}
    block_times: dict[int, int] = {}
    events: list[dict[str, Any]] = []
    for log in logs:
        trade = decode_trade(log, ibs_is_token0)
        if trade is None:
            continue
        if trade.tx_hash not in tx_from:
            tx_from[trade.tx_hash] = Web3.to_checksum_address(web3.eth.get_transaction(trade.tx_hash)["from"])
        if trade.block_number not in block_times:
            block_times[trade.block_number] = int(web3.eth.get_block(trade.block_number)["timestamp"])
        events.append({
            "id": f"{trade.tx_hash}:{trade.log_index}",
            "address": tx_from[trade.tx_hash],
            "side": trade.side,
            "ibs_raw": str(trade.ibs_raw),
            "usdt_raw": str(trade.usdt_raw),
            "timestamp": block_times[trade.block_number],
            "tx_hash": trade.tx_hash,
            "block_number": trade.block_number,
            "log_index": trade.log_index,
        })
    return events


def trim_events(events: Iterable[dict[str, Any]], now_ts: int, window_hours: int = WINDOW_HOURS) -> list[dict[str, Any]]:
    cutoff = now_ts - window_hours * 3600
    unique: dict[str, dict[str, Any]] = {}
    for event in events:
        if int(event["timestamp"]) >= cutoff:
            unique[str(event["id"])] = event
    return sorted(unique.values(), key=lambda x: (int(x["block_number"]), int(x["log_index"])))


def aggregate_addresses(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "buy_count": 0, "sell_count": 0,
        "buy_ibs_raw": 0, "sell_ibs_raw": 0,
        "buy_usdt_raw": 0, "sell_usdt_raw": 0,
        "timestamps": [], "last_tx_hash": "",
    })
    for event in events:
        key = str(event["address"]).lower()
        row = rows[key]
        row["address"] = Web3.to_checksum_address(event["address"])
        side = str(event["side"]).lower()
        row[f"{side}_count"] += 1
        row[f"{side}_ibs_raw"] += int(event["ibs_raw"])
        row[f"{side}_usdt_raw"] += int(event["usdt_raw"])
        row["timestamps"].append(int(event["timestamp"]))
        row["last_tx_hash"] = str(event["tx_hash"])

    result: list[dict[str, Any]] = []
    for row in rows.values():
        row["timestamps"].sort()
        row["total_count"] = row["buy_count"] + row["sell_count"]
        row["gross_usdt_raw"] = row["buy_usdt_raw"] + row["sell_usdt_raw"]
        row["net_sell_usdt_raw"] = row["sell_usdt_raw"] - row["buy_usdt_raw"]
        row["net_sell_ibs_raw"] = row["sell_ibs_raw"] - row["buy_ibs_raw"]
        row["first_ts"] = row["timestamps"][0]
        row["last_ts"] = row["timestamps"][-1]
        row["avg_interval_minutes"] = (
            Decimal(row["last_ts"] - row["first_ts"]) / Decimal(60 * (row["total_count"] - 1))
            if row["total_count"] > 1 else None
        )
        result.append(row)
    return sorted(result, key=lambda x: (x["total_count"], x["gross_usdt_raw"]), reverse=True)


def frequency_label(row: dict[str, Any]) -> str | None:
    buys, sells, total = row["buy_count"], row["sell_count"], row["total_count"]
    if total >= MIN_TOTAL_TRADES and buys >= MIN_SIDE_TRADES and sells >= MIN_SIDE_TRADES:
        matched_ratio = Decimal(min(row["buy_ibs_raw"], row["sell_ibs_raw"])) / Decimal(max(row["buy_ibs_raw"], row["sell_ibs_raw"])) if max(row["buy_ibs_raw"], row["sell_ibs_raw"]) else Decimal(0)
        if matched_ratio >= Decimal("0.7") and row["avg_interval_minutes"] is not None and row["avg_interval_minutes"] <= 120:
            return "🔄 高频双向周转"
        return "🔁 高频双向交易"
    if sells >= MIN_ONE_WAY_TRADES:
        return "🔴 高频卖出"
    if buys >= MIN_ONE_WAY_TRADES:
        return "🟢 高频买入"
    return None


def qualifying_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row, label=label) for row in rows if (label := frequency_label(row))]


def fmt_short_address(address: str) -> str:
    return f"{address[:8]}…{address[-6:]}"


def fmt_time(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%m-%d %H:%M")


def build_summary(rows: list[dict[str, Any]], all_rows: list[dict[str, Any]], ibs_decimals: int, usdt_decimals: int, now_ts: int) -> str:
    total_buys = sum(x["buy_count"] for x in all_rows)
    total_sells = sum(x["sell_count"] for x in all_rows)
    market_net = sum(x["net_sell_usdt_raw"] for x in all_rows)
    market_icon = "🔴" if market_net > 0 else "🟢"
    lines = [
        f"📊 <b>IBS高频地址榜｜近{WINDOW_HOURS}小时</b>",
        f"全池成交：买 {total_buys} 笔 / 卖 {total_sells} 笔",
        f"{market_icon} 交易净抛压：<b>{amount(market_net, usdt_decimals):+,.2f} USDT</b>",
        f"高频地址：<b>{len(rows)}</b> 个（双向≥{MIN_TOTAL_TRADES}笔；单边≥{MIN_ONE_WAY_TRADES}笔）",
    ]
    if not rows:
        lines.extend(["", "当前没有达到高频门槛的地址。"])
    for index, row in enumerate(rows[:TOP_LIMIT], 1):
        net = amount(row["net_sell_usdt_raw"], usdt_decimals)
        net_text = f"净卖出 {net:+,.2f} USDT" if net >= 0 else f"净买入 {abs(net):,.2f} USDT"
        interval = "--" if row["avg_interval_minutes"] is None else f"{row['avg_interval_minutes']:.0f}分钟"
        address = row["address"]
        lines.extend([
            "",
            f"<b>{index}. {html.escape(row['label'])}</b> ｜ <a href=\"https://bscscan.com/address/{address}\">{fmt_short_address(address)}</a>",
            f"交易 {row['total_count']} 笔（买{row['buy_count']} / 卖{row['sell_count']}）｜平均间隔 {interval}",
            f"买/卖：{amount(row['buy_ibs_raw'], ibs_decimals):,.2f} / {amount(row['sell_ibs_raw'], ibs_decimals):,.2f} IBS",
            f"{net_text} ｜ 成交额 {amount(row['gross_usdt_raw'], usdt_decimals):,.2f} USDT",
            f"活跃：{fmt_time(row['first_ts'])} → {fmt_time(row['last_ts'])} UTC",
        ])
    lines.extend([
        "",
        "说明：净卖出为该地址卖出获得USDT减去买入支付USDT；正数代表从LP净拿走USDT。高频仅描述交易行为，不等于套利或老鼠仓。",
        f"统计时间：{fmt_time(now_ts)} UTC",
    ])
    return "\n".join(lines)


def build_new_alert(row: dict[str, Any], ibs_decimals: int, usdt_decimals: int) -> str:
    address = row["address"]
    net = amount(row["net_sell_usdt_raw"], usdt_decimals)
    direction = f"净卖出 {net:+,.2f} USDT" if net >= 0 else f"净买入 {abs(net):,.2f} USDT"
    return "\n".join([
        "⚡️ <b>新增IBS高频地址</b>",
        f"类型：<b>{html.escape(row['label'])}</b>",
        f"地址：<code>{address}</code>",
        f"近{WINDOW_HOURS}小时：{row['total_count']}笔（买{row['buy_count']} / 卖{row['sell_count']}）",
        f"买/卖：{amount(row['buy_ibs_raw'], ibs_decimals):,.2f} / {amount(row['sell_ibs_raw'], ibs_decimals):,.2f} IBS",
        f"资金方向：<b>{direction}</b>",
        f"成交额：{amount(row['gross_usdt_raw'], usdt_decimals):,.2f} USDT",
        f'<a href="https://bscscan.com/address/{address}">查看地址</a> ｜ <a href="https://bscscan.com/tx/{row["last_tx_hash"]}">最新交易</a>',
        "说明：达到高频统计门槛不代表套利；需结合独立卖家画像判断资金来源与真实盈亏。",
    ])


def send_telegram(token: str, chat_id: str, message: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=TELEGRAM_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError(f"Telegram发送失败：{response.text}")


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


def main() -> None:
    rpc_url = require_env("BSC_RPC")
    bot_token = require_env("BOT_TOKEN")
    chat_id = require_env("CHAT_ID")
    web3 = connect_web3(rpc_url)
    ibs_is_token0, ibs_decimals, usdt_decimals, _, _, _ = current_pair_meta(web3)
    state = load_state()
    confirmed_block = int(web3.eth.block_number) - CONFIRMATION_BLOCKS
    first_run = state.get("last_scanned_block") is None
    start = max(1, confirmed_block - BOOTSTRAP_BLOCKS + 1) if first_run else int(state["last_scanned_block"]) + 1
    end = min(confirmed_block, start + MAX_SCAN_BLOCKS - 1)
    now_ts = utc_now_ts()

    new_events: list[dict[str, Any]] = []
    if start <= end:
        new_events = collect_events(web3, get_swap_logs(web3, start, end), ibs_is_token0)
        state["last_scanned_block"] = end
    state["events"] = trim_events(list(state.get("events", [])) + new_events, now_ts)
    all_rows = aggregate_addresses(state["events"])
    high_rows = qualifying_rows(all_rows)
    state["latest_ranking"] = [
        {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in row.items()
            if key != "timestamps"
        }
        for row in high_rows
    ]

    alerted = state.setdefault("alerted", {})
    current_keys = {row["address"].lower() for row in high_rows}
    if first_run:
        for key in current_keys:
            alerted[key] = now_ts
    else:
        for row in reversed(high_rows):
            key = row["address"].lower()
            if key not in alerted:
                send_telegram(bot_token, chat_id, build_new_alert(row, ibs_decimals, usdt_decimals))
                alerted[key] = now_ts
    for key in list(alerted):
        if key not in current_keys and now_ts - int(alerted[key]) > WINDOW_HOURS * 3600:
            del alerted[key]

    last_report = int(state.get("last_report_ts", 0))
    if first_run or now_ts - last_report >= REPORT_MINUTES * 60:
        send_telegram(bot_token, chat_id, build_summary(high_rows, all_rows, ibs_decimals, usdt_decimals, now_ts))
        state["last_report_ts"] = now_ts
    save_state(state)
    print(f"扫描区块 {start}-{end}，新增{len(new_events)}笔Swap；近{WINDOW_HOURS}小时高频地址{len(high_rows)}个")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"IBS高频地址统计失败：{exc}", flush=True)
        raise
