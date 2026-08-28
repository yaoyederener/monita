#!/usr/bin/env python3
"""Daily IBS/USDT pool, treasury (USDT + BTCB), and RBS ledger."""

from __future__ import annotations

import bisect
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

BTCB_ADDRESS = Web3.to_checksum_address("0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c")
WBNB_ADDRESS = Web3.to_checksum_address("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c")
PANCAKE_V2_ROUTER = Web3.to_checksum_address("0x10ED43C718714eb63d5aA57B78B54704E256024E")
BTCB_DECIMALS = 18

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
BALANCE_GROUPS = (*GROUPS, "treasury_btcb")


def default_bucket() -> dict[str, Any]:
    bucket: dict[str, Any] = {
        "buy_count": 0,
        "sell_count": 0,
        "buy_ibs_raw": "0",
        "sell_ibs_raw": "0",
        "buy_usdt_raw": "0",
        "sell_usdt_raw": "0",
        "treasury_btcb_total_in_raw": "0",
        "treasury_btcb_total_out_raw": "0",
        "internal_usdt_raw": "0",
        "large_external_outflows": [],
    }
    for group in GROUPS:
        for suffix in ("total_in", "total_out", "external_in", "external_out", "internal_in", "internal_out"):
            bucket[f"{group}_{suffix}_raw"] = "0"
    return bucket


def default_state() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "last_scanned_block": None,
        "last_btcb_scanned_block": None,
        "btcb_coverage_start_ts": None,
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


def get_watched_token_transfers(
    web3: Web3,
    token_address: str,
    watched_addresses: Iterable[str],
    from_block: int,
    to_block: int,
) -> list[Any]:
    watched_topics = [topic_address(address) for address in watched_addresses]
    outgoing = get_logs_chunked(
        web3,
        {"address": token_address, "topics": [TRANSFER_TOPIC, watched_topics]},
        from_block,
        to_block,
    )
    incoming = get_logs_chunked(
        web3,
        {"address": token_address, "topics": [TRANSFER_TOPIC, None, watched_topics]},
        from_block,
        to_block,
    )
    unique = {
        f"{Web3.to_hex(log['transactionHash'])}:{int(log['logIndex'])}": log
        for log in outgoing + incoming
    }
    return sorted(unique.values(), key=lambda log: (int(log["blockNumber"]), int(log["logIndex"])))


def get_watched_usdt_transfers(web3: Web3, from_block: int, to_block: int) -> list[Any]:
    return get_watched_token_transfers(web3, USDT_ADDRESS, WATCHED_ADDRESSES, from_block, to_block)


def get_watched_btcb_transfers(web3: Web3, from_block: int, to_block: int) -> list[Any]:
    return get_watched_token_transfers(
        web3,
        BTCB_ADDRESS,
        TREASURY_ADDRESSES.values(),
        from_block,
        to_block,
    )


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


def record_btcb_transfer(bucket: dict[str, Any], source: str, destination: str, value: int) -> None:
    treasury = {address.lower() for address in TREASURY_ADDRESSES.values()}
    if source.lower() in treasury:
        add_raw(bucket, "treasury_btcb_total_out_raw", value)
    if destination.lower() in treasury:
        add_raw(bucket, "treasury_btcb_total_in_raw", value)


def aggregate_logs(
    state: dict[str, Any],
    swap_logs: Iterable[Any],
    transfer_logs: Iterable[Any],
    btcb_transfer_logs: Iterable[Any],
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
    aggregate_btcb_logs(state, btcb_transfer_logs, boundary_blocks, boundary_days)


def aggregate_btcb_logs(
    state: dict[str, Any],
    btcb_transfer_logs: Iterable[Any],
    boundary_blocks: list[int],
    boundary_days: list[str],
) -> None:
    for log in btcb_transfer_logs:
        day = day_for_block(int(log["blockNumber"]), boundary_blocks, boundary_days)
        record_btcb_transfer(
            ensure_bucket(state, day),
            topic_to_address(log["topics"][1]),
            topic_to_address(log["topics"][2]),
            data_to_int(log["data"]),
        )


def sum_balances(contract: Any, addresses: Iterable[str]) -> int:
    return sum(int(contract.functions.balanceOf(address).call()) for address in addresses)


def current_balances(web3: Web3) -> dict[str, int]:
    erc20_abi = [
        {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    ]
    usdt = web3.eth.contract(address=USDT_ADDRESS, abi=erc20_abi)
    btcb = web3.eth.contract(address=BTCB_ADDRESS, abi=erc20_abi)
    return {
        "lp": int(usdt.functions.balanceOf(PAIR_ADDRESS).call()),
        "treasury": sum_balances(usdt, TREASURY_ADDRESSES.values()),
        "rbs": sum_balances(usdt, RBS_ADDRESSES.values()),
        "treasury_btcb": sum_balances(btcb, TREASURY_ADDRESSES.values()),
    }


def current_btcb_price_usdt(web3: Web3, usdt_decimals: int) -> Decimal | None:
    router = web3.eth.contract(address=PANCAKE_V2_ROUTER, abi=[
        {
            "inputs": [
                {"name": "amountIn", "type": "uint256"},
                {"name": "path", "type": "address[]"},
            ],
            "name": "getAmountsOut",
            "outputs": [{"name": "amounts", "type": "uint256[]"}],
            "stateMutability": "view",
            "type": "function",
        },
    ])
    quotes: list[Decimal] = []
    for path in ([BTCB_ADDRESS, USDT_ADDRESS], [BTCB_ADDRESS, WBNB_ADDRESS, USDT_ADDRESS]):
        try:
            result = router.functions.getAmountsOut(10**BTCB_DECIMALS, path).call()
            quotes.append(amount(int(result[-1]), usdt_decimals))
        except Exception as exc:
            print(f"BTCB链上估值路径不可用：{path}（{exc}）", flush=True)
    if quotes:
        return max(quotes)
    try:
        response = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=TELEGRAM_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return Decimal(response.json()["price"])
    except Exception as exc:
        print(f"BTCB估值暂不可用：{exc}", flush=True)
        return None


def reconstruct_balances(state: dict[str, Any], closing_balances: dict[str, int]) -> None:
    cursor = dict(closing_balances)
    for day in sorted(state.get("daily", {}), reverse=True):
        bucket = ensure_bucket(state, day)
        bucket["closing_balances_raw"] = {group: str(cursor[group]) for group in BALANCE_GROUPS}
        opening: dict[str, int] = {}
        for group in BALANCE_GROUPS:
            prefix = "treasury_btcb" if group == "treasury_btcb" else group
            net = int(bucket[f"{prefix}_total_in_raw"]) - int(bucket[f"{prefix}_total_out_raw"])
            opening[group] = cursor[group] - net
        bucket["opening_balances_raw"] = {group: str(opening[group]) for group in BALANCE_GROUPS}
        cursor = opening


def raw(bucket: dict[str, Any], key: str) -> int:
    return int(bucket.get(key, "0"))


def balance_raw(bucket: dict[str, Any], boundary: str, group: str) -> int:
    return int(bucket.get(f"{boundary}_balances_raw", {}).get(group, "0"))


def usdt(value_raw: int, decimals: int) -> str:
    return f"{amount(value_raw, decimals):,.2f} USDT"


def btcb(value_raw: int) -> str:
    return f"{amount(value_raw, BTCB_DECIMALS):,.8f} BTCB"


def directional_usdt(value_raw: int, decimals: int) -> str:
    if value_raw > 0:
        return f"增加 {usdt(value_raw, decimals)}"
    if value_raw < 0:
        return f"减少 {usdt(-value_raw, decimals)}"
    return "无变化"


def directional_btcb(value_raw: int) -> str:
    if value_raw > 0:
        return f"增加 {btcb(value_raw)}"
    if value_raw < 0:
        return f"减少 {btcb(-value_raw)}"
    return "无变化"


def build_report(
    day: str,
    bucket: dict[str, Any],
    ibs_decimals: int,
    usdt_decimals: int,
    btcb_price_usdt: Decimal | None = None,
    partial: bool = False,
) -> str:
    buy_usdt = raw(bucket, "buy_usdt_raw")
    sell_usdt = raw(bucket, "sell_usdt_raw")
    trade_net = buy_usdt - sell_usdt
    lp_open, lp_close = balance_raw(bucket, "opening", "lp"), balance_raw(bucket, "closing", "lp")
    treasury_open, treasury_close = balance_raw(bucket, "opening", "treasury"), balance_raw(bucket, "closing", "treasury")
    btcb_open = balance_raw(bucket, "opening", "treasury_btcb")
    btcb_close = balance_raw(bucket, "closing", "treasury_btcb")
    rbs_open, rbs_close = balance_raw(bucket, "opening", "rbs"), balance_raw(bucket, "closing", "rbs")
    lp_delta = lp_close - lp_open
    treasury_delta = treasury_close - treasury_open
    btcb_delta = btcb_close - btcb_open
    rbs_delta = rbs_close - rbs_open
    btcb_open_value = 0
    btcb_close_value = 0
    if btcb_price_usdt is not None:
        scale = Decimal(10) ** usdt_decimals
        btcb_open_value = int(amount(btcb_open, BTCB_DECIMALS) * btcb_price_usdt * scale)
        btcb_close_value = int(amount(btcb_close, BTCB_DECIMALS) * btcb_price_usdt * scale)
    combined_open = lp_open + treasury_open + rbs_open + btcb_open_value
    combined_close = lp_close + treasury_close + rbs_close + btcb_close_value
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
        f"买卖结果：<b>{directional_usdt(trade_net, usdt_decimals)}</b>",
        f"其他余额变化：{directional_usdt(non_trade_lp, usdt_decimals)}（非买卖造成）",
        f"LP当日总变化：<b>{directional_usdt(lp_delta, usdt_decimals)}</b>",
        "",
        "<b>国库资金（含BTC）</b>",
        f"USDT：{usdt(treasury_open, usdt_decimals)} → {usdt(treasury_close, usdt_decimals)}｜{directional_usdt(treasury_delta, usdt_decimals)}",
        f"BTC：{btcb(btcb_open)} → {btcb(btcb_close)}｜{directional_btcb(btcb_delta)}",
        "",
        "<b>RBS余额</b>",
        f"日初/当前：{usdt(rbs_open, usdt_decimals)} → {usdt(rbs_close, usdt_decimals)}",
        f"当日变化：<b>{directional_usdt(rbs_delta, usdt_decimals)}</b>",
        "",
        "<b>项目总资金</b>",
        f"LP+国库+RBS：{usdt(combined_open, usdt_decimals)} → {usdt(combined_close, usdt_decimals)}",
        f"当日总变化：<b>{directional_usdt(combined_delta, usdt_decimals)}</b>",
    ]
    if btcb_price_usdt is not None:
        treasury_open_value = treasury_open + btcb_open_value
        treasury_close_value = treasury_close + btcb_close_value
        lines[lines.index("<b>RBS余额</b>"):lines.index("<b>RBS余额</b>")] = [
            f"BTC当前估价：{btcb_price_usdt:,.2f} USDT",
            f"国库总价值：{usdt(treasury_open_value, usdt_decimals)} → {usdt(treasury_close_value, usdt_decimals)}",
            "",
        ]
        lines.extend(["", "说明：BTC按本次报告时的BTCB/USDT价格统一估值，因此总变化反映余额变化，不反映BTC价格涨跌。"])
    else:
        lines[-2] = "LP+国库USDT+RBS（暂不含BTC估值）：" + lines[-2].split("：", 1)[1]
        lines.extend(["", "说明：BTC价格暂时无法取得，项目总资金暂不含BTC估值；BTC数量仍正常监控。"])
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
        aggregate_logs(state, swap_logs, transfer_logs, [], ibs_is_token0, boundaries, days)
        state["last_scanned_block"] = end
        print(
            f"扫描区块 {start}-{end}：Swap {len(swap_logs)}笔，"
            f"相关USDT转账 {len(transfer_logs)}笔",
            flush=True,
        )
    if state.get("btcb_coverage_start_ts") is None:
        # BTCB tracking was added after the USDT ledger had accumulated weeks of
        # history. Only yesterday and today are needed for the next complete and
        # current reports; scanning the entire legacy window delays Telegram for
        # many scheduled runs.
        btcb_start_ts = local_midnight_ts(local_now.date() - timedelta(days=1))
        state["btcb_coverage_start_ts"] = btcb_start_ts
        state["last_btcb_scanned_block"] = None
    else:
        btcb_start_ts = int(state["btcb_coverage_start_ts"])
    if state.get("last_btcb_scanned_block") is None:
        btcb_start = find_block_at_or_after(web3, btcb_start_ts, confirmed_block)
    else:
        btcb_start = int(state["last_btcb_scanned_block"]) + 1
    btcb_end = min(confirmed_block, btcb_start + MAX_SCAN_BLOCKS - 1)
    if btcb_start <= btcb_end:
        btcb_boundaries, btcb_days = block_day_boundaries(web3, btcb_start, btcb_end)
        btcb_transfer_logs = get_watched_btcb_transfers(web3, btcb_start, btcb_end)
        aggregate_btcb_logs(state, btcb_transfer_logs, btcb_boundaries, btcb_days)
        state["last_btcb_scanned_block"] = btcb_end
        print(
            f"扫描BTCB区块 {btcb_start}-{btcb_end}：国库相关转账 {len(btcb_transfer_logs)}笔",
            flush=True,
        )
    usdt_backlog = confirmed_block - int(state.get("last_scanned_block") or 0)
    btcb_backlog = confirmed_block - int(state.get("last_btcb_scanned_block") or 0)
    if usdt_backlog > 0 or btcb_backlog > 0:
        save_state(state)
        print(
            f"历史数据尚未追平：USDT/交易剩余约 {max(usdt_backlog, 0)} 个区块，"
            f"BTCB剩余约 {max(btcb_backlog, 0)} 个区块",
            flush=True,
        )
        return
    reconstruct_balances(state, current_balances(web3))
    btcb_price = current_btcb_price_usdt(web3, usdt_decimals)
    force_report = os.getenv("DAILY_FUNDS_FORCE_REPORT", "").strip().lower() in {"1", "true", "yes", "on"}
    target = report_due(state, local_now)
    if target is not None:
        send_telegram(
            bot_token,
            chat_id,
            build_report(target, ensure_bucket(state, target), ibs_decimals, usdt_decimals, btcb_price),
        )
        state["last_reported_date"] = target
    if force_report or (target is None and current_report_due(state, local_now)):
        today = local_now.date().isoformat()
        send_telegram(
            bot_token,
            chat_id,
            build_report(
                today,
                ensure_bucket(state, today),
                ibs_decimals,
                usdt_decimals,
                btcb_price,
                partial=True,
            ),
        )
        state["last_current_report_ts"] = int(local_now.timestamp())
        state["last_current_report_slot"] = local_now.strftime("%Y-%m-%dT%H%z")
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"IBS每日资金监控失败：{exc}", flush=True)
        raise
