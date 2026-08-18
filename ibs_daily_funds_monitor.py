#!/usr/bin/env python3
"""Daily IBS/USDT pool, treasury, and RBS USDT ledger."""

from __future__ import annotations

import bisect
import html
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests
from web3 import Web3

from ibs_wallet_profiler import (
    CONFIRMATION_BLOCKS,
    IBS_ADDRESS,
    PAIR_ADDRESS,
    TRANSFER_TOPIC,
    USDT_ADDRESS,
    amount,
    connect_web3,
    current_pair_meta,
    decode_trade,
    get_swap_logs,
)


STATE_FILE = Path(os.getenv("DAILY_FUNDS_STATE_FILE", "data/ibs_daily_funds_state.json"))
LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "Asia/Shanghai"))
REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "0"))
REPORT_MINUTE = int(os.getenv("DAILY_REPORT_MINUTE", "10"))
MAX_SCAN_BLOCKS = int(os.getenv("DAILY_FUNDS_MAX_SCAN_BLOCKS", "220000"))
LOG_CHUNK_SIZE = int(os.getenv("DAILY_FUNDS_LOG_CHUNK_SIZE", "3000"))
TELEGRAM_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "20"))
LARGE_EXTERNAL_OUTFLOW_USDT = Decimal(os.getenv("LARGE_EXTERNAL_OUTFLOW_USDT", "10000"))

TREASURY_ADDRESSES: dict[str, str] = {
    "Safety Treasury": "0x5BB0d5Cb2276a054d933B14D023A2063CF8F28Ce",
    "BTCB Treasury": "0xE9A7c7Bb2D4264940296d5D6C414d09DD37627F0",
    "Worldpool Treasury": "0x7266256440a32f5dA2691B1EF98Fffb5b655658a",
}
RBS_ADDRESSES: dict[str, str] = {
    "RBS Stabilizer": "0xCBA922f6aff0EC8CB0703D44249456Ef779A394C",
    "RBS Executor": "0xf3fc289ABbfF1F847649bC738A4e39D2ED365711",
}
TREASURY_ADDRESSES = {label: Web3.to_checksum_address(address) for label, address in TREASURY_ADDRESSES.items()}
RBS_ADDRESSES = {label: Web3.to_checksum_address(address) for label, address in RBS_ADDRESSES.items()}

GROUP_BY_ADDRESS = {PAIR_ADDRESS.lower(): "lp"}
GROUP_BY_ADDRESS.update({address.lower(): "treasury" for address in TREASURY_ADDRESSES.values()})
GROUP_BY_ADDRESS.update({address.lower(): "rbs" for address in RBS_ADDRESSES.values()})
WATCHED_ADDRESSES = tuple(Web3.to_checksum_address(address) for address in GROUP_BY_ADDRESS)
GROUPS = ("lp", "treasury", "rbs")


def default_bucket() -> dict[str, Any]:
    bucket: dict[str, Any] = {
        "buy_count": 0,
        "sell_count": 0,
        "buy_ibs_raw": "0",
        "sell_ibs_raw": "0",
        "buy_usdt_raw": "0",
        "sell_usdt_raw": "0",
        "internal_usdt_raw": "0",
        "large_external_outflows": [],
    }
    for group in GROUPS:
        for suffix in ("total_in", "total_out", "external_in", "external_out", "internal_in", "internal_out"):
            bucket[f"{group}_{suffix}_raw"] = "0"
    return bucket


def default_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "last_scanned_block": None,
        "coverage_start_ts": None,
        "daily": {},
        "last_reported_date": None,
        "last_current_report_ts": None,
        "last_current_report_slot": None,
    }


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return default_state()
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    merged = default_state()
    merged.update(state)
    merged.setdefault("daily", {})
    return merged


def save_state(state: dict[str, Any]) -> None:
    cutoff = (datetime.now(LOCAL_TZ).date() - timedelta(days=45)).isoformat()
    state["daily"] = {key: value for key, value in state.get("daily", {}).items() if key >= cutoff}
    state["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    temp.replace(STATE_FILE)


def ensure_bucket(state: dict[str, Any], day: str) -> dict[str, Any]:
    bucket = state.setdefault("daily", {}).setdefault(day, default_bucket())
    defaults = default_bucket()
    for key, value in defaults.items():
        bucket.setdefault(key, value)
    return bucket


def add_raw(bucket: dict[str, Any], key: str, value: int) -> None:
    bucket[key] = str(int(bucket.get(key, "0")) + int(value))


def topic_address(address: str) -> str:
    return "0x" + "0" * 24 + address.lower().removeprefix("0x")


def topic_to_address(topic: Any) -> str:
    return Web3.to_checksum_address("0x" + Web3.to_hex(topic)[-40:])


def data_to_int(data: Any) -> int:
    raw = bytes(data) if not isinstance(data, str) else bytes.fromhex(data.removeprefix("0x"))
    return int.from_bytes(raw, "big")


def get_logs_chunked(web3: Web3, query: dict[str, Any], from_block: int, to_block: int) -> list[Any]:
    logs: list[Any] = []
    start = from_block
    chunk = LOG_CHUNK_SIZE
    while start <= to_block:
        end = min(to_block, start + chunk - 1)
        try:
            logs.extend(web3.eth.get_logs(dict(query, fromBlock=start, toBlock=end)))
            start = end + 1
        except Exception:
            if chunk <= 20:
                raise
            chunk = max(20, chunk // 2)
    return logs


def get_watched_usdt_transfers(web3: Web3, from_block: int, to_block: int) -> list[Any]:
    watched_topics = [topic_address(address) for address in WATCHED_ADDRESSES]
    outgoing = get_logs_chunked(
        web3,
        {"address": USDT_ADDRESS, "topics": [TRANSFER_TOPIC, watched_topics]},
        from_block,
        to_block,
    )
    incoming = get_logs_chunked(
        web3,
        {"address": USDT_ADDRESS, "topics": [TRANSFER_TOPIC, None, watched_topics]},
        from_block,
        to_block,
    )
    unique = {
        f"{Web3.to_hex(log['transactionHash'])}:{int(log['logIndex'])}": log
        for log in outgoing + incoming
    }
    return sorted(unique.values(), key=lambda log: (int(log["blockNumber"]), int(log["logIndex"])))


def block_timestamp(web3: Web3, block_number: int) -> int:
    return int(web3.eth.get_block(block_number)["timestamp"])


def find_block_at_or_after(web3: Web3, target_ts: int, high: int) -> int:
    low = 1
    while low < high:
        mid = (low + high) // 2
        if block_timestamp(web3, mid) < target_ts:
            low = mid + 1
        else:
            high = mid
    return low


def local_midnight_ts(day: date) -> int:
    return int(datetime.combine(day, time.min, tzinfo=LOCAL_TZ).timestamp())


def block_day_boundaries(web3: Web3, start: int, end: int) -> tuple[list[int], list[str]]:
    start_day = datetime.fromtimestamp(block_timestamp(web3, start), LOCAL_TZ).date()
    end_day = datetime.fromtimestamp(block_timestamp(web3, end), LOCAL_TZ).date()
    days: list[date] = []
    cursor = start_day
    while cursor <= end_day:
        days.append(cursor)
        cursor += timedelta(days=1)
    blocks = [find_block_at_or_after(web3, local_midnight_ts(day), end) for day in days]
    return blocks, [day.isoformat() for day in days]


def day_for_block(block_number: int, boundary_blocks: list[int], boundary_days: list[str]) -> str:
    index = max(0, bisect.bisect_right(boundary_blocks, block_number) - 1)
    return boundary_days[index]


def record_trade(bucket: dict[str, Any], trade: Any) -> None:
    side = trade.side.lower()
    bucket[f"{side}_count"] = int(bucket.get(f"{side}_count", 0)) + 1
    add_raw(bucket, f"{side}_ibs_raw", trade.ibs_raw)
    add_raw(bucket, f"{side}_usdt_raw", trade.usdt_raw)


def record_transfer(bucket: dict[str, Any], source: str, destination: str, value: int, tx_hash: str) -> None:
    source_group = GROUP_BY_ADDRESS.get(source.lower())
    destination_group = GROUP_BY_ADDRESS.get(destination.lower())
    is_internal = source_group is not None and destination_group is not None
    if is_internal:
        add_raw(bucket, "internal_usdt_raw", value)
    if source_group is not None:
        add_raw(bucket, f"{source_group}_total_out_raw", value)
        add_raw(bucket, f"{source_group}_{'internal' if destination_group else 'external'}_out_raw", value)
    if destination_group is not None:
        add_raw(bucket, f"{destination_group}_total_in_raw", value)
        add_raw(bucket, f"{destination_group}_{'internal' if source_group else 'external'}_in_raw", value)
    if source_group in {"treasury", "rbs"} and destination_group is None:
        bucket.setdefault("large_external_outflows", []).append({
            "group": source_group,
            "amount_raw": str(value),
            "to": destination,
            "tx_hash": tx_hash,
        })


def aggregate_logs(
    state: dict[str, Any],
    swap_logs: Iterable[Any],
    transfer_logs: Iterable[Any],
    ibs_is_token0: bool,
    boundary_blocks: list[int],
    boundary_days: list[str],
) -> None:
    for log in swap_logs:
        trade = decode_trade(log, ibs_is_token0)
        if trade is not None:
            record_trade(ensure_bucket(state, day_for_block(trade.block_number, boundary_blocks, boundary_days)), trade)
    for log in transfer_logs:
        day = day_for_block(int(log["blockNumber"]), boundary_blocks, boundary_days)
        record_transfer(
            ensure_bucket(state, day),
            topic_to_address(log["topics"][1]),
            topic_to_address(log["topics"][2]),
            data_to_int(log["data"]),
            Web3.to_hex(log["transactionHash"]),
        )


def sum_balances(contract: Any, addresses: Iterable[str]) -> int:
    return sum(int(contract.functions.balanceOf(address).call()) for address in addresses)


def current_balances(web3: Web3) -> dict[str, int]:
    usdt = web3.eth.contract(address=USDT_ADDRESS, abi=[
        {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    ])
    return {
        "lp": int(usdt.functions.balanceOf(PAIR_ADDRESS).call()),
        "treasury": sum_balances(usdt, TREASURY_ADDRESSES.values()),
        "rbs": sum_balances(usdt, RBS_ADDRESSES.values()),
    }


def reconstruct_balances(state: dict[str, Any], closing_balances: dict[str, int]) -> None:
    cursor = dict(closing_balances)
    for day in sorted(state.get("daily", {}), reverse=True):
        bucket = ensure_bucket(state, day)
        bucket["closing_balances_raw"] = {group: str(cursor[group]) for group in GROUPS}
        opening: dict[str, int] = {}
        for group in GROUPS:
            net = int(bucket[f"{group}_total_in_raw"]) - int(bucket[f"{group}_total_out_raw"])
            opening[group] = cursor[group] - net
        bucket["opening_balances_raw"] = {group: str(opening[group]) for group in GROUPS}
        cursor = opening


def raw(bucket: dict[str, Any], key: str) -> int:
    return int(bucket.get(key, "0"))


def balance_raw(bucket: dict[str, Any], boundary: str, group: str) -> int:
    return int(bucket.get(f"{boundary}_balances_raw", {}).get(group, "0"))


def signed_usdt(value_raw: int, decimals: int) -> str:
    return f"{amount(value_raw, decimals):+,.2f} USDT"


def usdt(value_raw: int, decimals: int) -> str:
    return f"{amount(value_raw, decimals):,.2f} USDT"


def build_report(day: str, bucket: dict[str, Any], ibs_decimals: int, usdt_decimals: int, partial: bool = False) -> str:
    buy_usdt = raw(bucket, "buy_usdt_raw")
    sell_usdt = raw(bucket, "sell_usdt_raw")
    trade_net = buy_usdt - sell_usdt
    lp_open, lp_close = balance_raw(bucket, "opening", "lp"), balance_raw(bucket, "closing", "lp")
    treasury_open, treasury_close = balance_raw(bucket, "opening", "treasury"), balance_raw(bucket, "closing", "treasury")
    rbs_open, rbs_close = balance_raw(bucket, "opening", "rbs"), balance_raw(bucket, "closing", "rbs")
    lp_delta = lp_close - lp_open
    treasury_delta = treasury_close - treasury_open
    rbs_delta = rbs_close - rbs_open
    combined_open = lp_open + treasury_open + rbs_open
    combined_close = lp_close + treasury_close + rbs_close
    combined_delta = combined_close - combined_open
    non_trade_lp = lp_delta - trade_net
    label = "截至当前" if partial else "完整日报"
    lines = [
        f"📊 <b>IBS项目每日资金报告｜{day}</b>",
        f"口径：北京时间 · {label}",
        "",
        "<b>IBS/USDT池</b>",
        f"日初/当前：{usdt(lp_open, usdt_decimals)} → {usdt(lp_close, usdt_decimals)}",
        f"买单：{bucket['buy_count']}笔｜{amount(raw(bucket, 'buy_ibs_raw'), ibs_decimals):,.2f} IBS｜流入 {usdt(buy_usdt, usdt_decimals)}",
        f"卖单：{bucket['sell_count']}笔｜{amount(raw(bucket, 'sell_ibs_raw'), ibs_decimals):,.2f} IBS｜流出 {usdt(sell_usdt, usdt_decimals)}",
        f"交易净流量：<b>{signed_usdt(trade_net, usdt_decimals)}</b>",
        f"LP实际变化：<b>{signed_usdt(lp_delta, usdt_decimals)}</b>",
        f"非交易调整：{signed_usdt(non_trade_lp, usdt_decimals)}",
        f"交易净消耗：{usdt(max(-trade_net, 0), usdt_decimals)}",
        "",
        "<b>国库资金</b>",
        f"日初/当前：{usdt(treasury_open, usdt_decimals)} → {usdt(treasury_close, usdt_decimals)}",
        f"外部流入/流出：{usdt(raw(bucket, 'treasury_external_in_raw'), usdt_decimals)} / {usdt(raw(bucket, 'treasury_external_out_raw'), usdt_decimals)}",
        f"净变化：<b>{signed_usdt(treasury_delta, usdt_decimals)}</b>",
        "",
        "<b>RBS余额</b>",
        f"日初/当前：{usdt(rbs_open, usdt_decimals)} → {usdt(rbs_close, usdt_decimals)}",
        f"外部流入/流出：{usdt(raw(bucket, 'rbs_external_in_raw'), usdt_decimals)} / {usdt(raw(bucket, 'rbs_external_out_raw'), usdt_decimals)}",
        f"净变化：<b>{signed_usdt(rbs_delta, usdt_decimals)}</b>",
        "",
        "<b>项目总资金</b>",
        f"LP+国库+RBS：{usdt(combined_open, usdt_decimals)} → {usdt(combined_close, usdt_decimals)}",
        f"总净变化：<b>{signed_usdt(combined_delta, usdt_decimals)}</b>",
        f"项目总净消耗：<b>{usdt(max(-combined_delta, 0), usdt_decimals)}</b>",
        f"内部划转：{usdt(raw(bucket, 'internal_usdt_raw'), usdt_decimals)}",
    ]
    large = [
        item for item in bucket.get("large_external_outflows", [])
        if amount(int(item["amount_raw"]), usdt_decimals) >= LARGE_EXTERNAL_OUTFLOW_USDT
    ]
    if large:
        lines.extend(["", f"<b>大额外部转出（≥{LARGE_EXTERNAL_OUTFLOW_USDT:,.0f} USDT）</b>"])
        for item in sorted(large, key=lambda row: int(row["amount_raw"]), reverse=True)[:5]:
            label_cn = "国库" if item["group"] == "treasury" else "RBS"
            destination = item["to"]
            lines.append(
                f"{label_cn} → <a href=\"https://bscscan.com/address/{destination}\">{destination[:8]}…{destination[-6:]}</a>：{usdt(int(item['amount_raw']), usdt_decimals)}"
            )
    lines.extend(["", "说明：项目总资金按LP、国库和RBS的链上USDT余额合计；三者之间的内部划转不会重复计入总消耗。"])
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, message: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=TELEGRAM_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram发送失败：{response.text}")
    result = payload.get("result", {})
    chat = result.get("chat", {})
    target = chat.get("title") or chat.get("username") or chat.get("first_name") or "未知会话"
    print(f"Telegram已发送：目标={target}（{chat.get('type', 'unknown')}），message_id={result.get('message_id', 'unknown')}", flush=True)


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


def report_due(state: dict[str, Any], now: datetime) -> str | None:
    if (now.hour, now.minute) < (REPORT_HOUR, REPORT_MINUTE):
        return None
    target = (now.date() - timedelta(days=1)).isoformat()
    if state.get("last_reported_date") == target or target not in state.get("daily", {}):
        return None
    return target


def current_report_due(state: dict[str, Any], now: datetime) -> bool:
    return state.get("last_current_report_slot") != now.strftime("%Y-%m-%dT%H%z")


def main() -> None:
    rpc_url = require_env("BSC_RPC")
    bot_token = require_env("BOT_TOKEN")
    chat_id = require_env("CHAT_ID")
    web3 = connect_web3(rpc_url)
    ibs_is_token0, ibs_decimals, usdt_decimals, _, _, _ = current_pair_meta(web3)
    state = load_state()
    confirmed_block = int(web3.eth.block_number) - CONFIRMATION_BLOCKS
    local_now = datetime.now(LOCAL_TZ)
    if state.get("last_scanned_block") is None:
        start_ts = local_midnight_ts(local_now.date())
        start = find_block_at_or_after(web3, start_ts, confirmed_block)
        state["coverage_start_ts"] = start_ts
    else:
        start = int(state["last_scanned_block"]) + 1
    end = min(confirmed_block, start + MAX_SCAN_BLOCKS - 1)
    if start <= end:
        boundaries, days = block_day_boundaries(web3, start, end)
        swap_logs = get_swap_logs(web3, start, end)
        transfer_logs = get_watched_usdt_transfers(web3, start, end)
        aggregate_logs(state, swap_logs, transfer_logs, ibs_is_token0, boundaries, days)
        state["last_scanned_block"] = end
        print(f"扫描区块 {start}-{end}：Swap {len(swap_logs)}笔，相关USDT转账 {len(transfer_logs)}笔", flush=True)
    if int(state.get("last_scanned_block") or 0) < confirmed_block:
        save_state(state)
        print(f"历史数据尚未追平，剩余约 {confirmed_block - int(state['last_scanned_block'])} 个区块", flush=True)
        return
    reconstruct_balances(state, current_balances(web3))
    force_report = os.getenv("DAILY_FUNDS_FORCE_REPORT", "").strip().lower() in {"1", "true", "yes", "on"}
    target = report_due(state, local_now)
    if target is not None:
        send_telegram(bot_token, chat_id, build_report(target, ensure_bucket(state, target), ibs_decimals, usdt_decimals))
        state["last_reported_date"] = target
    if force_report or (target is None and current_report_due(state, local_now)):
        today = local_now.date().isoformat()
        send_telegram(bot_token, chat_id, build_report(today, ensure_bucket(state, today), ibs_decimals, usdt_decimals, partial=True))
        state["last_current_report_ts"] = int(local_now.timestamp())
        state["last_current_report_slot"] = local_now.strftime("%Y-%m-%dT%H%z")
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"IBS每日资金监控失败：{exc}", flush=True)
        raise
