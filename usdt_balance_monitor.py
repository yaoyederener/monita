#!/usr/bin/env python3
"""POTS treasury, market, and core-health monitor for GitHub Actions + Telegram.

The reported indicators include:
1. Treasury USDT = IBS/USDT LP reserve + RBS + Safety Treasury.
2. External USDT net flow across the known POTS protocol perimeter.
3. RBS drawdown plus treasury and RBS runway estimates.
4. IBS price, circulating-market-cap proxy, supply, and net issuance.
5. Rolling 24-hour IBS sell pressure from Pancake V2 Swap logs.
6. Visible-USDT coverage estimate per circulating IBS.
7. Confirmed single-swap IBS buy/sell alerts above a configurable threshold.

The monitor is read-only on BNB Smart Chain. It never needs a wallet key.
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware


PROGRAM_VERSION = "2026-08-09-large-trade-v5"
STATE_SCHEMA_VERSION = 4

IBS_ADDRESS = "0x255e746aBb8D9Acac00d6d023e5E63E3b8DFA7cd"
USDT_ADDRESS = "0x55d398326f99059ff775485246999027B3197955"
LP_ADDRESS = "0x2a4b99a9c4544d35e8d266111c50b67fea01d53d"
RBS_ADDRESS = "0xCBA922f6aff0EC8CB0703D44249456Ef779A394C"
SAFETY_ADDRESS = "0x5BB0d5Cb2276a054d933B14D023A2063CF8F28Ce"

PROTOCOL_ADDRESSES = {
    "IBS/USDT LP": LP_ADDRESS,
    "RBS Stabilizer": RBS_ADDRESS,
    "Safety Treasury": SAFETY_ADDRESS,
    "Bonding": "0x89E6EFd26aF347fD7f1Eb9846a21E4e85311CC30",
    "Operator Bond": "0xb83d56a4de0f080d9a0ccb7B67e747af68bbC655",
    "Staking": "0x6025FC9840Cc4e282125a74F4b00dC5038A8058f",
    "Release Turbine": "0x004202D0b1759BcDBD939BC5a2BfBCEeD9DD34b1",
    "IBS AEM": "0xE72a413864B8f795f2a1c2de4176e4BE9BF56F34",
    "Rebase Pool": "0xC274041Bf5d9487baB196E63e6609B6161FFCD7d",
    "BTCB Treasury": "0xE9A7c7Bb2D4264940296d5D6C414d09DD37627F0",
    "Worldpool Treasury": "0x7266256440a32f5dA2691B1EF98Fffb5b655658a",
    "Legacy Worldpool Treasury": "0xc407928502e0aa6D313494EC2EE224DB55DcF1FC",
    "RBS Executor": "0xf3fc289ABbfF1F847649bC738A4e39D2ED365711",
}

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DEAD_ADDRESS = "0x000000000000000000000000000000000000dEaD"

STATE_FILE = Path("data/usdt_balance.json")
HISTORY_FILE = Path("data/usdt_flow_history.csv")


def int_env(name: str, default: str, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, default))
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {name} 不是有效整数") from exc
    if value < minimum:
        raise RuntimeError(f"环境变量 {name} 不能小于 {minimum}")
    return value


def decimal_env(name: str, default: str) -> Decimal:
    try:
        value = Decimal(os.getenv(name, default))
    except InvalidOperation as exc:
        raise RuntimeError(f"环境变量 {name} 不是有效数字") from exc
    if not value.is_finite() or value < 0:
        raise RuntimeError(f"环境变量 {name} 必须是大于等于0的有限数字")
    return value


RPC_TIMEOUT_SECONDS = int_env("RPC_TIMEOUT_SECONDS", "30", 1)
TELEGRAM_TIMEOUT_SECONDS = int_env("TELEGRAM_TIMEOUT_SECONDS", "20", 1)
CONFIRMATION_BLOCKS = int_env("CONFIRMATION_BLOCKS", "3", 0)
REPORT_INTERVAL_MINUTES = int_env("REPORT_INTERVAL_MINUTES", "60", 5)
IMMEDIATE_TOTAL_CHANGE_USDT = decimal_env("IMMEDIATE_TOTAL_CHANGE_USDT", "100000")
CRITICAL_OUTFLOW_USDT = decimal_env("CRITICAL_OUTFLOW_USDT", "100000")
LOG_CHUNK_SIZE = int_env("LOG_CHUNK_SIZE", "2000", 100)
LOG_MAX_RPC_CALLS = int_env("LOG_MAX_RPC_CALLS", "200", 1)
LOG_SCAN_BUDGET_SECONDS = int_env("LOG_SCAN_BUDGET_SECONDS", "75", 10)
LARGE_TRADE_THRESHOLD_IBS = decimal_env("LARGE_TRADE_THRESHOLD_IBS", "200")
LARGE_TRADE_MAX_SCAN_BLOCKS = int_env("LARGE_TRADE_MAX_SCAN_BLOCKS", "20000", 100)
LARGE_TRADE_MESSAGE_BATCH_SIZE = 6
LARGE_TRADE_SEEN_LIMIT = 1000

ERC20_ABI = [
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "treasury",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "taxTreasury",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

PAIR_ABI = [
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token1",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getReserves",
        "outputs": [
            {"name": "reserve0", "type": "uint112"},
            {"name": "reserve1", "type": "uint112"},
            {"name": "blockTimestampLast", "type": "uint32"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

SWAP_TOPIC = Web3.to_hex(
    Web3.keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)")
)

@dataclass(frozen=True)
class CurrentSnapshot:
    observed_at: datetime
    block_time_utc: str
    block_number: int
    usdt_decimals: int
    ibs_decimals: int
    lp_usdt_raw: int
    lp_ibs_raw: int
    lp_balance_usdt_raw: int
    rbs_usdt_raw: int
    safety_usdt_raw: int
    treasury_usdt_raw: int
    # Compatibility field retained for existing state readers: LP + RBS only.
    total_usdt_raw: int
    protocol_usdt_raw: int
    ibs_total_supply_raw: int
    ibs_dead_raw: int
    ibs_circulating_raw: int
    ibs_is_token0: bool
    ibs_price_usdt: Decimal
    market_cap_usdt: Decimal
    backing_per_ibs: Decimal
    treasury_address: str
    tax_treasury_address: str
    protocol_addresses: Tuple[str, ...]
    protocol_config_hash: str


@dataclass
class FlowSummary:
    from_block: int
    to_block: int
    complete: bool
    external_net_raw: int = 0
    event_count: int = 0
    critical_events: List[Dict[str, Any]] = field(default_factory=list)
    config_changed: bool = False


@dataclass(frozen=True)
class WindowMetrics:
    label: str
    elapsed_days: Decimal
    total_delta_raw: int
    total_change: Decimal
    treasury_delta_raw: int
    treasury_change: Decimal
    rbs_delta_raw: int
    rbs_change: Decimal
    supply_delta_raw: int
    supply_change: Decimal
    backing_change: Decimal
    external_net_raw: Optional[int]
    start_block: int = 0
    end_block: int = 0


@dataclass(frozen=True)
class SellPressure:
    from_block: int
    to_block: int
    sell_ibs_raw: int
    sell_usdt_raw: int
    event_count: int


@dataclass(frozen=True)
class LargeTrade:
    event_id: str
    transaction_hash: str
    block_number: int
    transaction_index: int
    log_index: int
    side: str
    ibs_raw: int
    usdt_raw: int


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少 GitHub Secret：{name}")
    return value


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def decimal_from(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def token_amount(raw: int, decimals: int) -> Decimal:
    getcontext().prec = max(80, decimals + 50)
    return Decimal(raw) / (Decimal(10) ** decimals)


def exact_amount(raw: int, decimals: int) -> str:
    return format(token_amount(raw, decimals), "f")


def fmt_amount(raw: int, decimals: int, places: int = 2) -> str:
    return f"{token_amount(raw, decimals):,.{places}f}"


def fmt_signed_amount(raw: int, decimals: int, places: int = 2) -> str:
    sign = "+" if raw >= 0 else "-"
    return f"{sign}{fmt_amount(abs(raw), decimals, places)}"


def fmt_pct(value: Optional[Decimal]) -> str:
    if value is None:
        return "积累中"
    prefix = "+" if value > 0 else ""
    return f"{prefix}{value * 100:.2f}%"


def pct_change(current: Decimal, previous: Decimal) -> Decimal:
    if previous == 0:
        return Decimal("0")
    return (current - previous) / previous


def treasury_raw_from_record(record: Dict[str, Any]) -> int:
    value = record.get("treasury_usdt_raw")
    if value is not None:
        return int(value)
    components = (
        record.get("lp_usdt_raw"),
        record.get("rbs_usdt_raw"),
        record.get("safety_usdt_raw"),
    )
    if all(item is not None for item in components):
        return sum(int(item) for item in components)
    return int(record.get("total_usdt_raw", 0))


def calculate_market_data(
    lp_usdt_raw: int,
    lp_ibs_raw: int,
    usdt_decimals: int,
    ibs_decimals: int,
    circulating_raw: int,
) -> Tuple[Decimal, Decimal]:
    lp_ibs = token_amount(lp_ibs_raw, ibs_decimals)
    if lp_ibs <= 0:
        raise RuntimeError("LP中的IBS储备为0，无法计算价格和市值")
    price_usdt = token_amount(lp_usdt_raw, usdt_decimals) / lp_ibs
    market_cap_usdt = price_usdt * token_amount(circulating_raw, ibs_decimals)
    return price_usdt, market_cap_usdt


def decode_swap_amounts(data: Any) -> Tuple[int, int, int, int]:
    if isinstance(data, str):
        raw = bytes.fromhex(data.removeprefix("0x"))
    else:
        raw = bytes(data)
    if len(raw) != 128:
        raise RuntimeError(f"Swap日志数据长度异常：{len(raw)}")
    return tuple(
        int.from_bytes(raw[offset : offset + 32], byteorder="big")
        for offset in range(0, 128, 32)
    )  # type: ignore[return-value]


def summarize_sell_pressure_logs(
    logs: Sequence[Dict[str, Any]],
    ibs_is_token0: bool,
) -> Tuple[int, int, int]:
    sell_ibs_raw = 0
    sell_usdt_raw = 0
    event_count = 0
    for log in logs:
        amount0_in, amount1_in, amount0_out, amount1_out = decode_swap_amounts(
            log["data"]
        )
        ibs_in = amount0_in if ibs_is_token0 else amount1_in
        ibs_out = amount0_out if ibs_is_token0 else amount1_out
        usdt_in = amount1_in if ibs_is_token0 else amount0_in
        usdt_out = amount1_out if ibs_is_token0 else amount0_out
        net_ibs_in = max(0, ibs_in - ibs_out)
        net_usdt_out = max(0, usdt_out - usdt_in)
        if net_ibs_in > 0 and net_usdt_out > 0:
            sell_ibs_raw += net_ibs_in
            sell_usdt_raw += net_usdt_out
            event_count += 1
    return sell_ibs_raw, sell_usdt_raw, event_count


def normalize_transaction_hash(value: Any) -> str:
    if isinstance(value, str):
        tx_hash = value.lower()
    else:
        tx_hash = Web3.to_hex(value).lower()
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    if len(tx_hash) != 66:
        raise RuntimeError(f"Swap日志交易哈希长度异常：{tx_hash}")
    try:
        int(tx_hash[2:], 16)
    except ValueError as exc:
        raise RuntimeError(f"Swap日志交易哈希格式异常：{tx_hash}") from exc
    return tx_hash


def decode_large_trade_log(
    log: Dict[str, Any],
    ibs_is_token0: bool,
) -> Optional[LargeTrade]:
    amount0_in, amount1_in, amount0_out, amount1_out = decode_swap_amounts(
        log["data"]
    )
    ibs_in = amount0_in if ibs_is_token0 else amount1_in
    ibs_out = amount0_out if ibs_is_token0 else amount1_out
    usdt_in = amount1_in if ibs_is_token0 else amount0_in
    usdt_out = amount1_out if ibs_is_token0 else amount0_out

    ibs_net_to_pair = ibs_in - ibs_out
    usdt_net_to_pair = usdt_in - usdt_out
    if ibs_net_to_pair > 0 and usdt_net_to_pair < 0:
        side = "SELL"
        ibs_raw = ibs_net_to_pair
        usdt_raw = -usdt_net_to_pair
    elif ibs_net_to_pair < 0 and usdt_net_to_pair > 0:
        side = "BUY"
        ibs_raw = -ibs_net_to_pair
        usdt_raw = usdt_net_to_pair
    else:
        return None

    transaction_hash_value = log.get("transactionHash", log.get("transaction_hash"))
    block_number_value = log.get("blockNumber", log.get("block_number"))
    log_index_value = log.get("logIndex", log.get("log_index"))
    if (
        transaction_hash_value is None
        or block_number_value is None
        or log_index_value is None
    ):
        raise RuntimeError("Swap日志缺少 transactionHash、blockNumber 或 logIndex")
    transaction_hash = normalize_transaction_hash(transaction_hash_value)
    block_number = int(block_number_value)
    log_index = int(log_index_value)
    transaction_index = int(
        log.get("transactionIndex", log.get("transaction_index", 0))
    )
    event_id = f"56:{LP_ADDRESS.lower()}:{transaction_hash}:{log_index}"
    return LargeTrade(
        event_id=event_id,
        transaction_hash=transaction_hash,
        block_number=block_number,
        transaction_index=transaction_index,
        log_index=log_index,
        side=side,
        ibs_raw=ibs_raw,
        usdt_raw=usdt_raw,
    )


def find_large_trades(
    logs: Sequence[Dict[str, Any]],
    ibs_is_token0: bool,
    threshold_raw: int,
    ignored_event_ids: Sequence[str] = (),
) -> List[LargeTrade]:
    if threshold_raw <= 0:
        return []
    ignored = set(ignored_event_ids)
    found: List[LargeTrade] = []
    for log in logs:
        trade = decode_large_trade_log(log, ibs_is_token0)
        if trade is None or trade.ibs_raw <= threshold_raw:
            continue
        if trade.event_id in ignored:
            continue
        ignored.add(trade.event_id)
        found.append(trade)
    return sorted(
        found,
        key=lambda trade: (
            trade.block_number,
            trade.transaction_index,
            trade.log_index,
        ),
    )


def large_trade_to_record(trade: LargeTrade) -> Dict[str, Any]:
    return {
        "event_id": trade.event_id,
        "transaction_hash": trade.transaction_hash,
        "block_number": trade.block_number,
        "transaction_index": trade.transaction_index,
        "log_index": trade.log_index,
        "side": trade.side,
        "ibs_raw": str(trade.ibs_raw),
        "usdt_raw": str(trade.usdt_raw),
    }


def large_trade_from_record(record: Dict[str, Any]) -> LargeTrade:
    side = str(record.get("side", ""))
    if side not in {"BUY", "SELL"}:
        raise RuntimeError(f"大额交易待发送记录方向异常：{side}")
    transaction_hash = normalize_transaction_hash(record.get("transaction_hash"))
    log_index = int(record["log_index"])
    expected_event_id = f"56:{LP_ADDRESS.lower()}:{transaction_hash}:{log_index}"
    event_id = str(record.get("event_id", expected_event_id))
    if event_id != expected_event_id:
        raise RuntimeError("大额交易待发送记录 event_id 不匹配")
    return LargeTrade(
        event_id=event_id,
        transaction_hash=transaction_hash,
        block_number=int(record["block_number"]),
        transaction_index=int(record.get("transaction_index", 0)),
        log_index=log_index,
        side=side,
        ibs_raw=int(record["ibs_raw"]),
        usdt_raw=int(record["usdt_raw"]),
    )


def is_log_range_error(exc: Exception) -> bool:
    message = str(exc).lower()
    if any(
        marker in message
        for marker in ("rate limit", "too many requests", "429", "timed out", "timeout")
    ):
        return False
    return any(
        marker in message
        for marker in (
            "limit exceeded",
            "block range",
            "too many results",
            "query returned more than",
            "response size exceeded",
            "eth_getlogs is limited",
        )
    )


def get_swap_logs_adaptive(
    web3: Web3,
    from_block: int,
    to_block: int,
    rpc_calls: List[int],
    deadline: Optional[float] = None,
) -> List[Any]:
    if deadline is not None and time.monotonic() >= deadline:
        raise RuntimeError("Swap日志扫描超过时间预算，本次抛压降级为不可用")
    rpc_calls[0] += 1
    if rpc_calls[0] > LOG_MAX_RPC_CALLS:
        raise RuntimeError(
            f"Swap日志RPC调用超过上限{LOG_MAX_RPC_CALLS}，本次抛压降级为不可用"
        )
    try:
        return list(
            web3.eth.get_logs(
                {
                    "address": Web3.to_checksum_address(LP_ADDRESS),
                    "fromBlock": from_block,
                    "toBlock": to_block,
                    "topics": [SWAP_TOPIC],
                }
            )
        )
    except Exception as exc:
        # Only split errors that explicitly mean the requested block range is too
        # large. Authentication, network, timeout, and rate-limit failures must
        # fail fast so sell-pressure enrichment cannot block the core report.
        if not is_log_range_error(exc) or to_block - from_block + 1 <= 10:
            raise
        midpoint = (from_block + to_block) // 2
        return get_swap_logs_adaptive(
            web3, from_block, midpoint, rpc_calls, deadline
        ) + get_swap_logs_adaptive(
            web3, midpoint + 1, to_block, rpc_calls, deadline
        )


def read_sell_pressure(
    web3: Web3,
    from_block: int,
    to_block: int,
    ibs_is_token0: bool,
) -> SellPressure:
    if from_block > to_block:
        return SellPressure(from_block, to_block, 0, 0, 0)
    sell_ibs_raw = 0
    sell_usdt_raw = 0
    event_count = 0
    rpc_calls = [0]
    deadline = time.monotonic() + LOG_SCAN_BUDGET_SECONDS
    chunk_start = from_block
    while chunk_start <= to_block:
        chunk_end = min(chunk_start + LOG_CHUNK_SIZE - 1, to_block)
        logs = get_swap_logs_adaptive(
            web3, chunk_start, chunk_end, rpc_calls, deadline
        )
        chunk_ibs, chunk_usdt, chunk_count = summarize_sell_pressure_logs(
            logs, ibs_is_token0
        )
        sell_ibs_raw += chunk_ibs
        sell_usdt_raw += chunk_usdt
        event_count += chunk_count
        chunk_start = chunk_end + 1
    return SellPressure(
        from_block=from_block,
        to_block=to_block,
        sell_ibs_raw=sell_ibs_raw,
        sell_usdt_raw=sell_usdt_raw,
        event_count=event_count,
    )


def read_large_trades(
    web3: Web3,
    from_block: int,
    to_block: int,
    ibs_is_token0: bool,
    threshold_raw: int,
    ignored_event_ids: Sequence[str] = (),
) -> List[LargeTrade]:
    if from_block > to_block or threshold_raw <= 0:
        return []
    logs: List[Dict[str, Any]] = []
    rpc_calls = [0]
    deadline = time.monotonic() + LOG_SCAN_BUDGET_SECONDS
    chunk_start = from_block
    while chunk_start <= to_block:
        chunk_end = min(chunk_start + LOG_CHUNK_SIZE - 1, to_block)
        logs.extend(
            get_swap_logs_adaptive(
                web3,
                chunk_start,
                chunk_end,
                rpc_calls,
                deadline,
            )
        )
        chunk_start = chunk_end + 1
    return find_large_trades(
        logs,
        ibs_is_token0,
        threshold_raw,
        ignored_event_ids,
    )


def default_state() -> Dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "chain": "BNB Smart Chain",
        "created_at_utc": now_utc().isoformat(),
        "updated_at_utc": None,
        "latest": None,
        "snapshots": [],
        "legacy_rbs_raw_balance": None,
        "large_trade_alerts": None,
    }


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return default_state()
    content = STATE_FILE.read_text(encoding="utf-8").strip()
    if not content or content == "{}":
        return default_state()
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"状态文件JSON已损坏，为保护历史，本次不覆盖：{exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("状态文件格式错误：根节点不是JSON对象")

    if int(raw.get("schema_version", 0)) == STATE_SCHEMA_VERSION:
        state = default_state()
        state.update(raw)
        if not isinstance(state.get("snapshots"), list):
            raise RuntimeError("状态文件 snapshots 格式错误")
        return state

    # Preserve the existing RBS-only record and establish a new combined baseline.
    state = default_state()
    legacy_rbs = raw.get("rbs_raw_balance", raw.get("raw_balance"))
    if legacy_rbs is not None and str(legacy_rbs).strip():
        state["legacy_rbs_raw_balance"] = str(legacy_rbs)
    state["legacy_source_block"] = raw.get("block_number")
    state["legacy_source_updated_at_utc"] = raw.get("updated_at_utc")
    return state


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


def ensure_large_trade_alert_state(
    state: Dict[str, Any],
    current: CurrentSnapshot,
) -> bool:
    alert_state = state.get("large_trade_alerts")
    if alert_state is None:
        state["large_trade_alerts"] = {
            "tracking_started_at_utc": current.observed_at.isoformat(),
            "tracking_started_block": current.block_number,
            "last_scanned_block": current.block_number,
            "seen_event_ids": [],
            "pending": [],
            "last_alert_at_utc": None,
        }
        return True
    if not isinstance(alert_state, dict):
        raise RuntimeError("状态文件 large_trade_alerts 格式错误")
    if int(alert_state.get("tracking_started_block", 0)) <= 0:
        raise RuntimeError("状态文件缺少有效的 large_trade_alerts.tracking_started_block")
    if not isinstance(alert_state.get("seen_event_ids", []), list):
        raise RuntimeError("状态文件 large_trade_alerts.seen_event_ids 格式错误")
    if not isinstance(alert_state.get("pending", []), list):
        raise RuntimeError("状态文件 large_trade_alerts.pending 格式错误")
    return False


def large_trade_scan_range(
    state: Dict[str, Any],
    current_block: int,
) -> Optional[Tuple[int, int]]:
    alert_state = state["large_trade_alerts"]
    started_block = int(alert_state["tracking_started_block"])
    last_scanned_block = int(
        alert_state.get("last_scanned_block", started_block)
    )
    from_block = max(last_scanned_block + 1, 1)
    if from_block > current_block:
        return None
    to_block = min(
        current_block,
        from_block + LARGE_TRADE_MAX_SCAN_BLOCKS - 1,
    )
    return from_block, to_block


def large_trade_ignored_ids(state: Dict[str, Any]) -> List[str]:
    alert_state = state["large_trade_alerts"]
    seen = [str(value) for value in alert_state.get("seen_event_ids", [])]
    pending = [
        str(record.get("event_id"))
        for record in alert_state.get("pending", [])
        if isinstance(record, dict) and record.get("event_id")
    ]
    return seen + pending


def enqueue_large_trades(
    state: Dict[str, Any],
    trades: Sequence[LargeTrade],
) -> bool:
    if not trades:
        return False
    alert_state = state["large_trade_alerts"]
    pending = list(alert_state.get("pending", []))
    existing = set(large_trade_ignored_ids(state))
    changed = False
    for trade in trades:
        if trade.event_id in existing:
            continue
        pending.append(large_trade_to_record(trade))
        existing.add(trade.event_id)
        changed = True
    if changed:
        alert_state["pending"] = pending
    return changed


def pending_large_trades(state: Dict[str, Any]) -> List[LargeTrade]:
    alert_state = state["large_trade_alerts"]
    trades = [
        large_trade_from_record(record)
        for record in alert_state.get("pending", [])
    ]
    return sorted(
        trades,
        key=lambda trade: (
            trade.block_number,
            trade.transaction_index,
            trade.log_index,
        ),
    )


def mark_large_trades_sent(
    state: Dict[str, Any],
    trades: Sequence[LargeTrade],
    sent_at: datetime,
) -> None:
    sent_ids = {trade.event_id for trade in trades}
    alert_state = state["large_trade_alerts"]
    alert_state["pending"] = [
        record
        for record in alert_state.get("pending", [])
        if str(record.get("event_id")) not in sent_ids
    ]
    seen = [str(value) for value in alert_state.get("seen_event_ids", [])]
    seen.extend(trade.event_id for trade in trades)
    alert_state["seen_event_ids"] = list(dict.fromkeys(seen))[
        -LARGE_TRADE_SEEN_LIMIT:
    ]
    alert_state["last_alert_at_utc"] = sent_at.isoformat()
    alert_state["last_alert_block"] = max(trade.block_number for trade in trades)


def build_large_trade_message(
    trades: Sequence[LargeTrade],
    current: CurrentSnapshot,
) -> str:
    if not trades:
        raise ValueError("大额交易提醒至少需要一笔交易")
    threshold_text = format(LARGE_TRADE_THRESHOLD_IBS, "f")
    lines = [
        "🚨 <b>IBS大额买卖提醒</b>",
        f"发现 {len(trades)} 笔单笔超过 {threshold_text} IBS 的池侧成交。",
    ]
    for index, trade in enumerate(trades, start=1):
        side_icon = "🟢" if trade.side == "BUY" else "🔴"
        side_text = "大额买入" if trade.side == "BUY" else "大额卖出"
        ibs_amount = token_amount(trade.ibs_raw, current.ibs_decimals)
        usdt_amount = token_amount(trade.usdt_raw, current.usdt_decimals)
        execution_price = usdt_amount / ibs_amount
        tx_url = f"https://bscscan.com/tx/{trade.transaction_hash}"
        lines.extend(
            [
                "",
                f"{index}. {side_icon} <b>{side_text}</b>",
                f"IBS（池侧）：<b>{ibs_amount:,.4f}</b>",
                f"USDT（池侧）：<b>{usdt_amount:,.2f}</b>",
                f"成交均价：<b>{execution_price:,.4f} USDT/IBS</b>",
                f"确认区块：<code>{trade.block_number}</code>",
                f'<a href="{tx_url}">查看交易</a>',
            ]
        )
    lines.extend(
        [
            "",
            f"扫描至确认区块：<code>{current.block_number}</code>",
            "说明：数量按IBS/USDT交易池的Swap事件计算，若代币收税，可能与钱包实际到账略有差异。",
        ]
    )
    return "\n".join(lines)


def connect_web3(rpc_url: str) -> Web3:
    web3 = Web3(
        Web3.HTTPProvider(
            rpc_url,
            request_kwargs={"timeout": RPC_TIMEOUT_SECONDS},
        )
    )
    web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not web3.is_connected():
        raise RuntimeError("BSC RPC连接失败，请检查 BSC_RPC")
    chain_id = int(web3.eth.chain_id)
    if chain_id != 56:
        raise RuntimeError(f"网络错误：当前Chain ID={chain_id}，BSC主网应为56")
    return web3


def read_current_snapshot(web3: Web3) -> CurrentSnapshot:
    usdt_address = Web3.to_checksum_address(USDT_ADDRESS)
    ibs_address = Web3.to_checksum_address(IBS_ADDRESS)
    lp_address = Web3.to_checksum_address(LP_ADDRESS)
    rbs_address = Web3.to_checksum_address(RBS_ADDRESS)
    safety_address = Web3.to_checksum_address(SAFETY_ADDRESS)

    latest_block = int(web3.eth.block_number)
    block_number = latest_block - CONFIRMATION_BLOCKS
    if block_number < 0:
        raise RuntimeError("确认块数量大于当前链高度")

    usdt = web3.eth.contract(address=usdt_address, abi=ERC20_ABI)
    ibs = web3.eth.contract(address=ibs_address, abi=ERC20_ABI)
    pair = web3.eth.contract(address=lp_address, abi=PAIR_ABI)

    usdt_decimals = int(usdt.functions.decimals().call(block_identifier=block_number))
    ibs_decimals = int(ibs.functions.decimals().call(block_identifier=block_number))
    token0 = Web3.to_checksum_address(
        pair.functions.token0().call(block_identifier=block_number)
    )
    token1 = Web3.to_checksum_address(
        pair.functions.token1().call(block_identifier=block_number)
    )
    if {token0.lower(), token1.lower()} != {ibs_address.lower(), usdt_address.lower()}:
        raise RuntimeError("LP代币组成与官方IBS/USDT地址不一致，已停止以避免误报")

    lp_balance_usdt_raw = int(
        usdt.functions.balanceOf(lp_address).call(block_identifier=block_number)
    )
    rbs_usdt_raw = int(
        usdt.functions.balanceOf(rbs_address).call(block_identifier=block_number)
    )
    safety_usdt_raw = int(
        usdt.functions.balanceOf(safety_address).call(block_identifier=block_number)
    )

    reserve0, reserve1, _ = pair.functions.getReserves().call(
        block_identifier=block_number
    )
    ibs_is_token0 = token0.lower() == ibs_address.lower()
    lp_ibs_raw = int(reserve0 if ibs_is_token0 else reserve1)
    lp_usdt_raw = int(reserve1 if ibs_is_token0 else reserve0)

    ibs_total_supply_raw = int(
        ibs.functions.totalSupply().call(block_identifier=block_number)
    )
    ibs_dead_raw = int(
        ibs.functions.balanceOf(Web3.to_checksum_address(DEAD_ADDRESS)).call(
            block_identifier=block_number
        )
    )
    ibs_zero_raw = int(
        ibs.functions.balanceOf(Web3.to_checksum_address(ZERO_ADDRESS)).call(
            block_identifier=block_number
        )
    )
    ibs_circulating_raw = max(0, ibs_total_supply_raw - ibs_dead_raw - ibs_zero_raw)
    ibs_price_usdt, market_cap_usdt = calculate_market_data(
        lp_usdt_raw,
        lp_ibs_raw,
        usdt_decimals,
        ibs_decimals,
        ibs_circulating_raw,
    )

    treasury_address = Web3.to_checksum_address(
        ibs.functions.treasury().call(block_identifier=block_number)
    )
    tax_treasury_address = Web3.to_checksum_address(
        ibs.functions.taxTreasury().call(block_identifier=block_number)
    )
    protocol_addresses = {
        Web3.to_checksum_address(address)
        for address in PROTOCOL_ADDRESSES.values()
    }
    protocol_addresses.update({treasury_address, tax_treasury_address})
    protocol_address_tuple = tuple(
        sorted((address.lower() for address in protocol_addresses))
    )
    protocol_config_hash = hashlib.sha256(
        "|".join(protocol_address_tuple).encode("ascii")
    ).hexdigest()
    known_balances = {
        lp_address.lower(): lp_balance_usdt_raw,
        rbs_address.lower(): rbs_usdt_raw,
        safety_address.lower(): safety_usdt_raw,
    }
    for address in protocol_addresses:
        key = address.lower()
        if key not in known_balances:
            known_balances[key] = int(
                usdt.functions.balanceOf(address).call(block_identifier=block_number)
            )
    protocol_usdt_raw = sum(known_balances[address.lower()] for address in protocol_addresses)

    treasury_usdt_raw = lp_usdt_raw + rbs_usdt_raw + safety_usdt_raw
    treasury_value = token_amount(treasury_usdt_raw, usdt_decimals)
    circulating = token_amount(ibs_circulating_raw, ibs_decimals)
    backing_per_ibs = (
        treasury_value / circulating if circulating > 0 else Decimal("0")
    )

    block = web3.eth.get_block(block_number)
    block_time = datetime.fromtimestamp(int(block["timestamp"]), tz=timezone.utc)
    return CurrentSnapshot(
        observed_at=now_utc(),
        block_time_utc=block_time.isoformat(),
        block_number=block_number,
        usdt_decimals=usdt_decimals,
        ibs_decimals=ibs_decimals,
        lp_usdt_raw=lp_usdt_raw,
        lp_ibs_raw=lp_ibs_raw,
        lp_balance_usdt_raw=lp_balance_usdt_raw,
        rbs_usdt_raw=rbs_usdt_raw,
        safety_usdt_raw=safety_usdt_raw,
        treasury_usdt_raw=treasury_usdt_raw,
        total_usdt_raw=lp_usdt_raw + rbs_usdt_raw,
        protocol_usdt_raw=protocol_usdt_raw,
        ibs_total_supply_raw=ibs_total_supply_raw,
        ibs_dead_raw=ibs_dead_raw + ibs_zero_raw,
        ibs_circulating_raw=ibs_circulating_raw,
        ibs_is_token0=ibs_is_token0,
        ibs_price_usdt=ibs_price_usdt,
        market_cap_usdt=market_cap_usdt,
        backing_per_ibs=backing_per_ibs,
        treasury_address=treasury_address,
        tax_treasury_address=tax_treasury_address,
        protocol_addresses=protocol_address_tuple,
        protocol_config_hash=protocol_config_hash,
    )


def summarize_interval(
    previous_record: Optional[Dict[str, Any]],
    current: CurrentSnapshot,
) -> FlowSummary:
    if previous_record is None:
        return FlowSummary(
            from_block=current.block_number,
            to_block=current.block_number,
            complete=False,
        )
    from_block = int(previous_record["block_number"]) + 1
    config_changed = (
        previous_record.get("protocol_config_hash") != current.protocol_config_hash
    )
    has_protocol_baseline = previous_record.get("protocol_usdt_raw") is not None
    complete = has_protocol_baseline and not config_changed
    external_net_raw = (
        current.protocol_usdt_raw - int(previous_record["protocol_usdt_raw"])
        if complete
        else 0
    )
    critical_raw = int(
        CRITICAL_OUTFLOW_USDT * (Decimal(10) ** current.usdt_decimals)
    )
    critical_events: List[Dict[str, Any]] = []
    protected = (
        ("RBS", "rbs_usdt_raw", RBS_ADDRESS),
        ("Safety", "safety_usdt_raw", SAFETY_ADDRESS),
    )
    for name, field_name, address in protected:
        if previous_record.get(field_name) is None:
            continue
        drop_raw = int(previous_record[field_name]) - int(getattr(current, field_name))
        if critical_raw > 0 and drop_raw >= critical_raw:
            critical_events.append(
                {
                    "name": name,
                    "address": address,
                    "amount_raw": str(drop_raw),
                    "protocol_net_raw": str(external_net_raw) if complete else None,
                }
            )

    return FlowSummary(
        from_block=from_block,
        to_block=current.block_number,
        complete=complete,
        external_net_raw=external_net_raw,
        event_count=len(critical_events),
        critical_events=critical_events,
        config_changed=config_changed,
    )


def record_from_snapshot(snapshot: CurrentSnapshot, flow: FlowSummary) -> Dict[str, Any]:
    return {
        "timestamp_utc": snapshot.observed_at.isoformat(),
        "block_time_utc": snapshot.block_time_utc,
        "block_number": snapshot.block_number,
        "usdt_decimals": snapshot.usdt_decimals,
        "ibs_decimals": snapshot.ibs_decimals,
        "lp_usdt_raw": str(snapshot.lp_usdt_raw),
        "lp_ibs_raw": str(snapshot.lp_ibs_raw),
        "lp_balance_usdt_raw": str(snapshot.lp_balance_usdt_raw),
        "rbs_usdt_raw": str(snapshot.rbs_usdt_raw),
        "safety_usdt_raw": str(snapshot.safety_usdt_raw),
        "treasury_usdt_raw": str(snapshot.treasury_usdt_raw),
        "total_usdt_raw": str(snapshot.total_usdt_raw),
        "protocol_usdt_raw": str(snapshot.protocol_usdt_raw),
        "protocol_addresses": list(snapshot.protocol_addresses),
        "protocol_config_hash": snapshot.protocol_config_hash,
        "treasury_address": snapshot.treasury_address,
        "tax_treasury_address": snapshot.tax_treasury_address,
        "ibs_total_supply_raw": str(snapshot.ibs_total_supply_raw),
        "ibs_dead_raw": str(snapshot.ibs_dead_raw),
        "ibs_circulating_raw": str(snapshot.ibs_circulating_raw),
        "ibs_is_token0": snapshot.ibs_is_token0,
        "ibs_price_usdt": format(snapshot.ibs_price_usdt, "f"),
        "market_cap_usdt": format(snapshot.market_cap_usdt, "f"),
        "backing_per_ibs": format(snapshot.backing_per_ibs, "f"),
        "flow_from_block": flow.from_block,
        "flow_to_block": flow.to_block,
        "flow_complete": flow.complete,
        "external_net_raw": str(flow.external_net_raw),
        "flow_event_count": flow.event_count,
        "protocol_config_changed": flow.config_changed,
    }


def find_window_start(
    records: Sequence[Dict[str, Any]],
    current_time: datetime,
    window: timedelta,
) -> Optional[Dict[str, Any]]:
    target_elapsed = window.total_seconds()
    # Hourly snapshots should produce a genuinely near-24h/7d window. A tight
    # tolerance avoids labelling a 19.2h or 28.8h interval as "24h".
    tolerance = target_elapsed * 0.05
    candidates = []
    for record in records:
        record_time = parse_iso(record.get("timestamp_utc"))
        if record_time is None or record_time >= current_time:
            continue
        elapsed = (current_time - record_time).total_seconds()
        if abs(elapsed - target_elapsed) <= tolerance:
            candidates.append(record)
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: abs(
            (current_time - (parse_iso(item["timestamp_utc"]) or current_time)).total_seconds()
            - target_elapsed
        ),
    )


def calculate_window(
    records: Sequence[Dict[str, Any]],
    current_record: Dict[str, Any],
    label: str,
    window: timedelta,
) -> Optional[WindowMetrics]:
    current_time = parse_iso(current_record["timestamp_utc"])
    if current_time is None:
        return None
    start = find_window_start(records, current_time, window)
    if start is None:
        return None
    start_time = parse_iso(start["timestamp_utc"])
    assert start_time is not None
    elapsed_days = Decimal(str((current_time - start_time).total_seconds())) / Decimal("86400")

    total_current = int(current_record["total_usdt_raw"])
    total_start = int(start["total_usdt_raw"])
    treasury_current = treasury_raw_from_record(current_record)
    treasury_start = treasury_raw_from_record(start)
    rbs_current = int(current_record["rbs_usdt_raw"])
    rbs_start = int(start["rbs_usdt_raw"])
    supply_current = int(current_record["ibs_total_supply_raw"])
    supply_start = int(start["ibs_total_supply_raw"])
    backing_current = decimal_from(current_record["backing_per_ibs"])
    backing_start = decimal_from(start["backing_per_ibs"])

    current_config_hash = current_record.get("protocol_config_hash")
    window_records = [
        record
        for record in records
        if start_time
        <= (parse_iso(record.get("timestamp_utc")) or current_time + timedelta(days=1))
        <= current_time
    ]
    same_protocol_config = bool(current_config_hash) and bool(window_records) and all(
        record.get("protocol_config_hash") == current_config_hash
        for record in window_records
    )
    has_protocol_balances = (
        current_record.get("protocol_usdt_raw") is not None
        and start.get("protocol_usdt_raw") is not None
    )
    external_net = (
        int(current_record["protocol_usdt_raw"]) - int(start["protocol_usdt_raw"])
        if same_protocol_config and has_protocol_balances
        else None
    )

    return WindowMetrics(
        label=label,
        elapsed_days=elapsed_days,
        total_delta_raw=total_current - total_start,
        total_change=pct_change(Decimal(total_current), Decimal(total_start)),
        treasury_delta_raw=treasury_current - treasury_start,
        treasury_change=pct_change(
            Decimal(treasury_current), Decimal(treasury_start)
        ),
        rbs_delta_raw=rbs_current - rbs_start,
        rbs_change=pct_change(Decimal(rbs_current), Decimal(rbs_start)),
        supply_delta_raw=supply_current - supply_start,
        supply_change=pct_change(Decimal(supply_current), Decimal(supply_start)),
        backing_change=pct_change(backing_current, backing_start),
        external_net_raw=external_net,
        start_block=int(start["block_number"]),
        end_block=int(current_record["block_number"]),
    )


def estimate_rbs_runway(
    current_rbs_raw: int,
    metrics_24h: Optional[WindowMetrics],
    metrics_7d: Optional[WindowMetrics],
) -> Optional[Decimal]:
    drawdown_rates = [
        Decimal(-metrics.rbs_delta_raw) / metrics.elapsed_days
        for metrics in (metrics_24h, metrics_7d)
        if metrics is not None
        and metrics.elapsed_days > 0
        and metrics.rbs_delta_raw < 0
    ]
    if not drawdown_rates:
        return None
    # Use the faster recent drawdown so a sudden 24h deterioration is not hidden
    # by a calmer 7d average.
    daily_drawdown = max(drawdown_rates)
    if daily_drawdown <= 0:
        return None
    return Decimal(current_rbs_raw) / daily_drawdown


def estimate_treasury_runway(
    current_treasury_raw: int,
    metrics_24h: Optional[WindowMetrics],
    metrics_7d: Optional[WindowMetrics],
) -> Optional[Decimal]:
    drawdown_rates = [
        Decimal(-metrics.treasury_delta_raw) / metrics.elapsed_days
        for metrics in (metrics_24h, metrics_7d)
        if metrics is not None
        and metrics.elapsed_days > 0
        and metrics.treasury_delta_raw < 0
    ]
    if not drawdown_rates:
        return None
    daily_drawdown = max(drawdown_rates)
    if daily_drawdown <= 0:
        return None
    return Decimal(current_treasury_raw) / daily_drawdown


def classify_health(
    metrics_24h: Optional[WindowMetrics],
    metrics_7d: Optional[WindowMetrics],
    runway_days: Optional[Decimal],
) -> Tuple[str, str, List[str]]:
    if metrics_24h is None:
        return "BASELINE", "🔵 建立基线", ["等待至少24小时数据"]

    reasons: List[str] = []
    ext24_negative = (
        metrics_24h.external_net_raw is not None and metrics_24h.external_net_raw < 0
    )
    if metrics_24h.treasury_change < 0:
        reasons.append("国库资金下降")
    if ext24_negative:
        reasons.append("外部净流出")
    if metrics_24h.rbs_change < 0:
        reasons.append("RBS正在消耗")
    if metrics_24h.backing_change < 0:
        reasons.append("单币可见USDT覆盖下降")
    if metrics_24h.supply_delta_raw > 0:
        reasons.append("IBS净增发")

    if metrics_7d is not None:
        ext7_negative = (
            metrics_7d.external_net_raw is not None and metrics_7d.external_net_raw < 0
        )
        death_risk = (
            metrics_24h.treasury_change < 0
            and metrics_7d.treasury_change < 0
            and ext24_negative
            and ext7_negative
            and metrics_7d.rbs_change <= Decimal("-0.10")
            and metrics_7d.backing_change < 0
            and runway_days is not None
            and runway_days <= Decimal("14")
        )
        if death_risk:
            return "DEATH_SPIRAL_RISK", "🔴 死亡螺旋风险", reasons

        growth = (
            metrics_24h.treasury_change > 0
            and metrics_7d.treasury_change > 0
            and metrics_24h.external_net_raw is not None
            and metrics_24h.external_net_raw > 0
            and metrics_7d.external_net_raw is not None
            and metrics_7d.external_net_raw > 0
            and metrics_7d.rbs_change >= 0
            and metrics_7d.backing_change >= 0
        )
        if growth:
            return "GROWTH", "🟢 增长阶段", ["国库资金与外部资金均为正"]

    downtrend = (
        metrics_24h.treasury_change < 0
        and ext24_negative
        and metrics_24h.rbs_change < 0
    )
    if downtrend:
        return "DOWNTREND", "🟠 下行阶段", reasons
    return "MIXED", "🟡 观察阶段", reasons or ["指标方向不一致"]


def report_due(
    state: Dict[str, Any],
    current: CurrentSnapshot,
    flow: FlowSummary,
) -> Tuple[bool, str]:
    previous = state.get("latest")
    if previous is None:
        return True, "BASELINE"
    previous_block = int(previous["block_number"])
    if current.block_number < previous_block:
        raise RuntimeError(
            f"当前确认区块{current.block_number}早于上次区块{previous_block}，本次不覆盖状态"
        )
    if flow.config_changed:
        return True, "PROTOCOL_CONFIG_CHANGED"
    if flow.critical_events:
        return True, "CRITICAL_OUTFLOW"
    total_delta = current.total_usdt_raw - int(previous["total_usdt_raw"])
    treasury_delta = current.treasury_usdt_raw - treasury_raw_from_record(previous)
    threshold_raw = int(
        IMMEDIATE_TOTAL_CHANGE_USDT * (Decimal(10) ** current.usdt_decimals)
    )
    if threshold_raw > 0 and abs(total_delta) >= threshold_raw:
        return True, "IMMEDIATE_TOTAL_CHANGE"
    if threshold_raw > 0 and abs(treasury_delta) >= threshold_raw:
        return True, "IMMEDIATE_TREASURY_CHANGE"
    previous_time = parse_iso(previous.get("timestamp_utc"))
    if previous_time is None:
        return True, "MISSING_TIMESTAMP"
    if current.observed_at >= previous_time + timedelta(minutes=REPORT_INTERVAL_MINUTES):
        return True, "PERIODIC"
    return False, "WAITING"


def format_window_change(
    metrics: Optional[WindowMetrics],
    raw_field: str,
    pct_field: str,
    decimals: int,
) -> str:
    if metrics is None:
        return "积累中"
    raw = int(getattr(metrics, raw_field))
    pct = getattr(metrics, pct_field)
    return f"{fmt_signed_amount(raw, decimals)} ({fmt_pct(pct)})"


def format_external(metrics: Optional[WindowMetrics], decimals: int) -> str:
    if metrics is None:
        return "积累中"
    if metrics.external_net_raw is None:
        return "地址边界已重建，重新积累中"
    return fmt_signed_amount(metrics.external_net_raw, decimals)


def format_runway(runway_days: Optional[Decimal]) -> str:
    if runway_days is None:
        return "未持续消耗/数据积累中"
    if runway_days > Decimal("999"):
        return ">999天"
    return f"约{runway_days:.1f}天"


def fmt_signed_decimal(value: Decimal, places: int = 2) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{abs(value):,.{places}f}"


def format_daily_issuance(
    metrics_24h: Optional[WindowMetrics],
    current: CurrentSnapshot,
) -> str:
    if metrics_24h is None:
        return "积累中"
    issuance_ibs = token_amount(metrics_24h.supply_delta_raw, current.ibs_decimals)
    issuance_value = issuance_ibs * current.ibs_price_usdt
    return (
        f"{fmt_signed_decimal(issuance_ibs)} IBS"
        f"（按现价 {fmt_signed_decimal(issuance_value)} USDT）"
    )


def format_sell_pressure(
    pressure: Optional[SellPressure],
    metrics_24h: Optional[WindowMetrics],
    current: CurrentSnapshot,
) -> str:
    if metrics_24h is None:
        return "积累中"
    if pressure is None:
        return "暂时无法读取"
    return (
        f"{fmt_amount(pressure.sell_usdt_raw, current.usdt_decimals)} USDT"
        f"（{fmt_amount(pressure.sell_ibs_raw, current.ibs_decimals)} IBS，"
        f"{pressure.event_count}笔）"
    )


def build_telegram_message(
    current: CurrentSnapshot,
    metrics_24h: Optional[WindowMetrics],
    metrics_7d: Optional[WindowMetrics],
    flow: FlowSummary,
    health_label: str,
    health_reasons: Sequence[str],
    runway_days: Optional[Decimal],
    report_reason: str,
    treasury_runway_days: Optional[Decimal] = None,
    sell_pressure: Optional[SellPressure] = None,
) -> str:
    usdt_decimals = current.usdt_decimals
    ibs_decimals = current.ibs_decimals

    if (
        metrics_24h is None
        and not flow.critical_events
        and report_reason in {"BASELINE", "PERIODIC", "PROTOCOL_CONFIG_CHANGED"}
    ):
        lines = [
            "🔵 <b>POTS资金监控｜数据积累中</b>",
            "",
            (
                f"💰 <b>当前国库资金：{fmt_amount(current.treasury_usdt_raw, usdt_decimals)} "
                "USDT</b>"
            ),
            f"• LP：{fmt_amount(current.lp_usdt_raw, usdt_decimals)}",
            f"• RBS：{fmt_amount(current.rbs_usdt_raw, usdt_decimals)}",
            f"• Safety：{fmt_amount(current.safety_usdt_raw, usdt_decimals)}",
            "",
            (
                f"📊 IBS池内价格：<b>{current.ibs_price_usdt:,.4f} USDT</b>\n"
                f"流通市值估算：<b>{current.market_cap_usdt:,.2f} USDT</b>\n"
                f"当前总量：<b>{fmt_amount(current.ibs_total_supply_raw, ibs_decimals)} IBS</b>\n"
                f"可见USDT覆盖：<b>{current.backing_per_ibs:.4f}/IBS</b>"
            ),
            "",
            "📌 24h净增发、每日抛压和国库运行时间正在积累。",
            "积累满24小时后显示短期数据，满7天后显示长期趋势。",
            "",
            f"区块：<code>{current.block_number}</code>",
        ]
        if flow.config_changed:
            lines.append("ℹ️ 监控地址已更新，正在重新积累对比数据。")
        if current.lp_balance_usdt_raw != current.lp_usdt_raw:
            unsynced = current.lp_balance_usdt_raw - current.lp_usdt_raw
            lines.append(
                f"⚠️ LP实际余额与reserve相差 "
                f"{fmt_signed_amount(unsynced, usdt_decimals)} USDT。"
            )
        return "\n".join(lines)

    total_24 = format_window_change(
        metrics_24h, "total_delta_raw", "total_change", usdt_decimals
    )
    total_7 = format_window_change(
        metrics_7d, "total_delta_raw", "total_change", usdt_decimals
    )
    treasury_24 = format_window_change(
        metrics_24h, "treasury_delta_raw", "treasury_change", usdt_decimals
    )
    treasury_7 = format_window_change(
        metrics_7d, "treasury_delta_raw", "treasury_change", usdt_decimals
    )
    rbs_24 = format_window_change(
        metrics_24h, "rbs_delta_raw", "rbs_change", usdt_decimals
    )
    rbs_7 = format_window_change(
        metrics_7d, "rbs_delta_raw", "rbs_change", usdt_decimals
    )
    issuance_24 = format_daily_issuance(metrics_24h, current)
    issuance_7 = (
        "积累中"
        if metrics_7d is None
        else fmt_signed_amount(metrics_7d.supply_delta_raw, ibs_decimals)
    )
    backing_24 = "积累中" if metrics_24h is None else fmt_pct(metrics_24h.backing_change)
    backing_7 = "积累中" if metrics_7d is None else fmt_pct(metrics_7d.backing_change)
    sell_pressure_24 = format_sell_pressure(sell_pressure, metrics_24h, current)

    lines = [
        f"<b>{health_label}｜POTS核心资金监控</b>",
        "",
        (
            f"① 国库资金：<b>{fmt_amount(current.treasury_usdt_raw, usdt_decimals)} USDT</b>\n"
            f"　LP {fmt_amount(current.lp_usdt_raw, usdt_decimals)} ｜ "
            f"RBS {fmt_amount(current.rbs_usdt_raw, usdt_decimals)} ｜ "
            f"Safety {fmt_amount(current.safety_usdt_raw, usdt_decimals)}\n"
            f"　24h {treasury_24} ｜ 7d {treasury_7}\n"
            f"　国库静态可持续时间：{format_runway(treasury_runway_days)}"
        ),
        (
            "② 已知协议地址净流入：\n"
            f"　24h {format_external(metrics_24h, usdt_decimals)} ｜ "
            f"7d {format_external(metrics_7d, usdt_decimals)}"
        ),
        (
            "③ IBS市值与总量：\n"
            f"　池内价格 <b>{current.ibs_price_usdt:,.4f} USDT</b> ｜ "
            f"流通市值估算 <b>{current.market_cap_usdt:,.2f} USDT</b>\n"
            f"　当前总量 {fmt_amount(current.ibs_total_supply_raw, ibs_decimals)} IBS ｜ "
            f"流通量 {fmt_amount(current.ibs_circulating_raw, ibs_decimals)} IBS"
        ),
        (
            "④ 每日增发与抛压：\n"
            f"　近24h净增发 {issuance_24}\n"
            f"　近24h真实抛压 {sell_pressure_24}"
        ),
        (
            f"⑤ 现有风险指标：\n"
            f"　LP+RBS 24h {total_24} ｜ 7d {total_7}\n"
            f"　RBS 24h {rbs_24} ｜ 7d {rbs_7} ｜ "
            f"支撑 {format_runway(runway_days)}\n"
            f"　可见USDT覆盖 <b>{current.backing_per_ibs:.4f}/IBS</b> ｜ "
            f"24h {backing_24} ｜ 7d {backing_7}\n"
            f"　净增发 7d {issuance_7} IBS"
        ),
        "",
        f"判断依据：{'、'.join(health_reasons[:5])}",
        f"确认区块：<code>{current.block_number}</code> ｜ 触发：{report_reason}",
    ]

    if flow.config_changed:
        lines.append("⚠️ 协议地址边界发生变化，净流入基线已重建，旧窗口暂不参与判断。")
    if current.lp_balance_usdt_raw != current.lp_usdt_raw:
        unsynced = current.lp_balance_usdt_raw - current.lp_usdt_raw
        lines.append(
            f"⚠️ LP实际余额与reserve相差 {fmt_signed_amount(unsynced, usdt_decimals)} USDT。"
        )

    if flow.critical_events:
        total_drop = sum(int(event["amount_raw"]) for event in flow.critical_events)
        lines.extend(
            [
                "",
                (
                    f"🚨 <b>金库大额余额下降：{len(flow.critical_events)}项，"
                    f"合计 {fmt_amount(total_drop, usdt_decimals)} USDT</b>"
                ),
            ]
        )
        for event in flow.critical_events[:3]:
            amount = fmt_amount(int(event["amount_raw"]), usdt_decimals)
            address_url = f"https://bscscan.com/address/{event['address']}"
            protocol_net = event.get("protocol_net_raw")
            net_text = (
                "边界基线重建中"
                if protocol_net is None
                else f"同期协议净额 {fmt_signed_amount(int(protocol_net), usdt_decimals)}"
            )
            lines.append(
                f"{html.escape(event['name'])} 余额下降 {amount} USDT ｜ "
                f"{net_text} ｜ <a href=\"{address_url}\">地址</a>"
            )
        if len(flow.critical_events) > 3:
            lines.append(f"另有 {len(flow.critical_events) - 3} 项未展开。")

    lp_url = f"https://bscscan.com/address/{LP_ADDRESS}"
    rbs_url = f"https://bscscan.com/address/{RBS_ADDRESS}"
    lines.extend(
        [
            "",
            f'<a href="{lp_url}">查看LP</a> ｜ <a href="{rbs_url}">查看RBS</a>',
        ]
    )
    return "\n".join(lines)


def send_telegram(bot_token: str, chat_id: str, message: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=TELEGRAM_TIMEOUT_SECONDS,
    )
    try:
        result = response.json()
    except ValueError:
        result = {"ok": False, "description": response.text[:300]}
    if not response.ok or not result.get("ok"):
        raise RuntimeError(
            f"Telegram推送失败：HTTP {response.status_code}，"
            f"{result.get('description', '未知错误')}"
        )


def send_pending_large_trade_alerts(
    state: Dict[str, Any],
    current: CurrentSnapshot,
    bot_token: str,
    chat_id: str,
    persist_state: bool = False,
) -> int:
    pending = pending_large_trades(state)
    sent_count = 0
    state_changed = persist_state
    for offset in range(0, len(pending), LARGE_TRADE_MESSAGE_BATCH_SIZE):
        batch = pending[offset : offset + LARGE_TRADE_MESSAGE_BATCH_SIZE]
        message = build_large_trade_message(batch, current)
        try:
            send_telegram(bot_token, chat_id, message)
        except Exception as exc:
            # Keep the batch in pending state so a later run can retry it even
            # after the rolling on-chain lookback has moved past the event.
            print(f"大额买卖Telegram提醒失败，保留待重试：{exc}")
            continue
        mark_large_trades_sent(state, batch, current.observed_at)
        sent_count += len(batch)
        state_changed = True
    if state_changed:
        atomic_write_json(STATE_FILE, state)
    return sent_count


def append_history(
    current: CurrentSnapshot,
    flow: FlowSummary,
    metrics_24h: Optional[WindowMetrics],
    metrics_7d: Optional[WindowMetrics],
    health_code: str,
    report_reason: str,
) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    exists = HISTORY_FILE.exists() and HISTORY_FILE.stat().st_size > 0
    row = {
        "timestamp_utc": current.observed_at.isoformat(),
        "block_time_utc": current.block_time_utc,
        "block_number": current.block_number,
        "lp_usdt": exact_amount(current.lp_usdt_raw, current.usdt_decimals),
        "lp_token_balance_usdt": exact_amount(
            current.lp_balance_usdt_raw, current.usdt_decimals
        ),
        "rbs_usdt": exact_amount(current.rbs_usdt_raw, current.usdt_decimals),
        "safety_usdt": exact_amount(current.safety_usdt_raw, current.usdt_decimals),
        "lp_plus_rbs_usdt": exact_amount(current.total_usdt_raw, current.usdt_decimals),
        "known_protocol_usdt": exact_amount(
            current.protocol_usdt_raw, current.usdt_decimals
        ),
        "interval_external_net_usdt": exact_amount(flow.external_net_raw, current.usdt_decimals),
        "protocol_config_hash": current.protocol_config_hash,
        "protocol_config_changed": flow.config_changed,
        "flow_complete": flow.complete,
        "ibs_total_supply": exact_amount(current.ibs_total_supply_raw, current.ibs_decimals),
        "ibs_circulating_proxy": exact_amount(current.ibs_circulating_raw, current.ibs_decimals),
        "backing_per_ibs": format(current.backing_per_ibs, "f"),
        "total_change_24h": "" if metrics_24h is None else format(metrics_24h.total_change, "f"),
        "total_change_7d": "" if metrics_7d is None else format(metrics_7d.total_change, "f"),
        "external_net_24h_usdt": ""
        if metrics_24h is None or metrics_24h.external_net_raw is None
        else exact_amount(metrics_24h.external_net_raw, current.usdt_decimals),
        "external_net_7d_usdt": ""
        if metrics_7d is None or metrics_7d.external_net_raw is None
        else exact_amount(metrics_7d.external_net_raw, current.usdt_decimals),
        "health": health_code,
        "report_reason": report_reason,
        "program_version": PROGRAM_VERSION,
    }
    with HISTORY_FILE.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def update_state(
    state: Dict[str, Any],
    record: Dict[str, Any],
    current: CurrentSnapshot,
) -> None:
    records = list(state.get("snapshots", []))
    records.append(record)
    cutoff = current.observed_at - timedelta(days=14)
    records = [
        item
        for item in records
        if (parse_iso(item.get("timestamp_utc")) or current.observed_at) >= cutoff
    ][-500:]

    state.update(
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "chain": "BNB Smart Chain",
            "token_contract": USDT_ADDRESS,
            "lp_address": LP_ADDRESS,
            "rbs_address": RBS_ADDRESS,
            "safety_address": SAFETY_ADDRESS,
            "treasury_address": current.treasury_address,
            "tax_treasury_address": current.tax_treasury_address,
            "protocol_addresses": list(current.protocol_addresses),
            "protocol_config_hash": current.protocol_config_hash,
            "latest": record,
            "snapshots": records,
            "updated_at_utc": current.observed_at.isoformat(),
            "program_version": PROGRAM_VERSION,
            # Legacy aliases preserve compatibility and old data readers.
            "wallet": RBS_ADDRESS,
            "raw_balance": str(current.rbs_usdt_raw),
            "decimals": current.usdt_decimals,
            "block_number": current.block_number,
        }
    )
    atomic_write_json(STATE_FILE, state)


def main() -> None:
    print(f"POTS核心资金监控启动，版本 {PROGRAM_VERSION}")
    rpc_url = require_env("BSC_RPC")
    bot_token = require_env("BOT_TOKEN")
    chat_id = require_env("CHAT_ID")

    state = load_state()
    previous_record = state.get("latest")
    web3 = connect_web3(rpc_url)
    current = read_current_snapshot(web3)
    flow = summarize_interval(previous_record, current)
    should_report, report_reason = report_due(state, current, flow)

    large_trade_ready = False
    large_trade_state_dirty = False
    if LARGE_TRADE_THRESHOLD_IBS > 0:
        try:
            initialized = ensure_large_trade_alert_state(state, current)
            large_trade_ready = True
            large_trade_state_dirty = initialized
            if initialized:
                print(
                    "IBS大额买卖提醒已建立当前确认区块基线；首次启用不补发历史交易"
                )
            else:
                scan_range = large_trade_scan_range(state, current.block_number)
                if scan_range is not None:
                    threshold_raw = int(
                        LARGE_TRADE_THRESHOLD_IBS
                        * (Decimal(10) ** current.ibs_decimals)
                    )
                    large_trades = read_large_trades(
                        web3,
                        scan_range[0],
                        scan_range[1],
                        current.ibs_is_token0,
                        threshold_raw,
                        large_trade_ignored_ids(state),
                    )
                    state["large_trade_alerts"]["last_scanned_block"] = scan_range[1]
                    # Persist partial catch-up progress immediately. When the scan
                    # reaches the current block, the cursor can ride along with the
                    # hourly report unless an alert itself changes the state.
                    if scan_range[1] < current.block_number:
                        large_trade_state_dirty = True
                    if enqueue_large_trades(state, large_trades):
                        large_trade_state_dirty = True
                    print(
                        f"大额买卖扫描区块={scan_range[0]}-{scan_range[1]}，"
                        f"新增待提醒={len(large_trades)}笔"
                    )
        except Exception as exc:
            # A log RPC problem must not suppress the existing core-funds report.
            print(f"读取IBS大额买卖失败，本次核心资金监控继续：{exc}")

    # Large-trade alerts are independent from the hourly funds report. Send them
    # first so report calculation or delivery problems cannot delay the alert.
    large_trade_sent_count = 0
    if large_trade_ready:
        large_trade_sent_count = send_pending_large_trade_alerts(
            state,
            current,
            bot_token,
            chat_id,
            persist_state=large_trade_state_dirty,
        )

    print(
        f"区块={current.block_number}, LP={fmt_amount(current.lp_usdt_raw, current.usdt_decimals)}, "
        f"RBS={fmt_amount(current.rbs_usdt_raw, current.usdt_decimals)}, "
        f"Safety={fmt_amount(current.safety_usdt_raw, current.usdt_decimals)}, "
        f"国库={fmt_amount(current.treasury_usdt_raw, current.usdt_decimals)} USDT"
    )
    print(
        f"已知协议USDT={fmt_amount(current.protocol_usdt_raw, current.usdt_decimals)}, "
        f"本周期边界净额={fmt_signed_amount(flow.external_net_raw, current.usdt_decimals)}, "
        f"边界一致={flow.complete}"
    )
    if not should_report:
        print(
            "尚未到核心资金报告时间且未触发资金阈值；"
            f"本次大额买卖提醒={large_trade_sent_count}笔，不移动资金报告基线"
        )
        return

    record = record_from_snapshot(current, flow)
    records = list(state.get("snapshots", [])) + [record]
    metrics_24h = calculate_window(
        records, record, "24h", timedelta(hours=24)
    )
    metrics_7d = calculate_window(records, record, "7d", timedelta(days=7))
    runway_days = estimate_rbs_runway(current.rbs_usdt_raw, metrics_24h, metrics_7d)
    treasury_runway_days = estimate_treasury_runway(
        current.treasury_usdt_raw, metrics_24h, metrics_7d
    )
    sell_pressure: Optional[SellPressure] = None
    if metrics_24h is not None:
        try:
            sell_pressure = read_sell_pressure(
                web3,
                metrics_24h.start_block + 1,
                metrics_24h.end_block,
                current.ibs_is_token0,
            )
            print(
                "近24h真实抛压="
                f"{fmt_amount(sell_pressure.sell_usdt_raw, current.usdt_decimals)} USDT, "
                f"{sell_pressure.event_count}笔"
            )
        except Exception as exc:  # Preserve the existing report if log RPC is unavailable.
            print(f"读取近24h Swap抛压失败，本次其余指标继续推送：{exc}")
    record["report_metrics"] = {
        "rbs_runway_days": None
        if runway_days is None
        else format(runway_days, "f"),
        "treasury_runway_days": None
        if treasury_runway_days is None
        else format(treasury_runway_days, "f"),
        "treasury_delta_24h_raw": None
        if metrics_24h is None
        else str(metrics_24h.treasury_delta_raw),
        "ibs_net_issuance_24h_raw": None
        if metrics_24h is None
        else str(metrics_24h.supply_delta_raw),
        "sell_pressure_24h": None
        if sell_pressure is None
        else {
            "from_block": sell_pressure.from_block,
            "to_block": sell_pressure.to_block,
            "sell_ibs_raw": str(sell_pressure.sell_ibs_raw),
            "sell_usdt_raw": str(sell_pressure.sell_usdt_raw),
            "event_count": sell_pressure.event_count,
        },
    }
    health_code, health_label, health_reasons = classify_health(
        metrics_24h, metrics_7d, runway_days
    )
    message = build_telegram_message(
        current,
        metrics_24h,
        metrics_7d,
        flow,
        health_label,
        health_reasons,
        runway_days,
        report_reason,
        treasury_runway_days=treasury_runway_days,
        sell_pressure=sell_pressure,
    )

    # Advance the baseline only after Telegram accepted the report.
    send_telegram(bot_token, chat_id, message)
    append_history(
        current,
        flow,
        metrics_24h,
        metrics_7d,
        health_code,
        report_reason,
    )
    update_state(state, record, current)
    print(f"Telegram报告成功：{health_code}，状态和CSV历史已保存")
    if large_trade_sent_count:
        print(f"IBS大额买卖提醒成功：{large_trade_sent_count}笔")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"运行失败：{type(exc).__name__}: {html.escape(str(exc))}", file=sys.stderr)
        raise
