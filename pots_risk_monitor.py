#!/usr/bin/env python3
"""POTS / IBS on-chain risk monitor for BNB Smart Chain.

Designed for GitHub Actions + Telegram. It complements the user's existing
large-trade monitor rather than replacing it.

The script monitors:
- IBS/USDT pool reserves, price, and an LP-backing proxy
- RBS and Safety Treasury USDT balances and depletion rate
- IBS mint / burn volume
- IBS buy / sell pressure from Pair Swap logs
- protocol-attributed buy share (best-effort attribution)
- large or unknown RBS / Safety Treasury outflows
- contract bytecode, EIP-1967 implementation slot, and owner changes
- daily risk score and practical withdrawal-stage guidance
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import requests
from web3 import Web3


# ---------------------------------------------------------------------------
# Addresses verified against the official POTS contract-address page.
# ---------------------------------------------------------------------------

IBS_ADDRESS = Web3.to_checksum_address(
    "0x255e746aBb8D9Acac00d6d023e5E63E3b8DFA7cd"
)
USDT_ADDRESS = Web3.to_checksum_address(
    "0x55d398326f99059fF775485246999027B3197955"
)
PAIR_ADDRESS = Web3.to_checksum_address(
    "0x2a4b99a9c4544d35e8d266111c50b67fea01d53d"
)

PROTOCOL_CONTRACTS: dict[str, str] = {
    "Bonding": "0x89E6EFd26aF347fD7f1Eb9846a21E4e85311CC30",
    "Operator Bond": "0xb83d56a4de0f080d9a0ccb7B67e747af68bbC655",
    "Staking": "0x6025FC9840Cc4e282125a74F4b00dC5038A8058f",
    "Release Turbine": "0x004202D0b1759BcDBD939BC5a2BfBCEeD9DD34b1",
    "IBS AEM": "0xE72a413864B8f795f2a1c2de4176e4BE9BF56F34",
    "Rebase Pool": "0xC274041Bf5d9487baB196E63e6609B6161FFCD7d",
    "Safety Treasury": "0x5BB0d5Cb2276a054d933B14D023A2063CF8F28Ce",
    "BTCB Treasury": "0xE9A7c7Bb2D4264940296d5D6C414d09DD37627F0",
    "Worldpool Treasury": "0x7266256440a32f5dA2691B1EF98Fffb5b655658a",
    "RBS Executor": "0xf3fc289ABbfF1F847649bC738A4e39D2ED365711",
    "RBS Stabilizer": "0xCBA922f6aff0EC8CB0703D44249456Ef779A394C",
}
PROTOCOL_CONTRACTS = {
    label: Web3.to_checksum_address(address)
    for label, address in PROTOCOL_CONTRACTS.items()
}

RBS_ADDRESS = PROTOCOL_CONTRACTS["RBS Stabilizer"]
SAFETY_ADDRESS = PROTOCOL_CONTRACTS["Safety Treasury"]
TURBINE_ADDRESS = PROTOCOL_CONTRACTS["Release Turbine"]
BONDING_ADDRESS = PROTOCOL_CONTRACTS["Bonding"]

PANCAKE_V2_ROUTER = Web3.to_checksum_address(
    "0x10ED43C718714eb63d5aA57B78B54704E256024E"
)
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DEAD_ADDRESS = "0x000000000000000000000000000000000000dEaD"

ADDRESS_LABELS: dict[str, str] = {
    address.lower(): label for label, address in PROTOCOL_CONTRACTS.items()
}
ADDRESS_LABELS.update(
    {
        IBS_ADDRESS.lower(): "IBS Token",
        USDT_ADDRESS.lower(): "USDT Token",
        PAIR_ADDRESS.lower(): "IBS/USDT Pair",
        PANCAKE_V2_ROUTER.lower(): "PancakeSwap Router",
        ZERO_ADDRESS.lower(): "Zero Address",
        DEAD_ADDRESS.lower(): "Dead Address",
    }
)

KNOWN_SAFE_RECIPIENTS = set(ADDRESS_LABELS)
PROTOCOL_ADDRESS_SET = {
    address.lower() for address in PROTOCOL_CONTRACTS.values()
}


# ---------------------------------------------------------------------------
# Runtime configuration
# ---------------------------------------------------------------------------

STATE_FILE = Path(os.getenv("POTS_STATE_FILE", "data/pots_risk_state.json"))
LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "America/Vancouver"))

CONFIRMATION_BLOCKS = int(os.getenv("CONFIRMATION_BLOCKS", "20"))
BLOCK_CHUNK_SIZE = int(os.getenv("BLOCK_CHUNK_SIZE", "10"))
MAX_BLOCKS_PER_RUN = int(os.getenv("MAX_BLOCKS_PER_RUN", "1200"))
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", "330"))
SNAPSHOT_INTERVAL_MINUTES = int(os.getenv("SNAPSHOT_INTERVAL_MINUTES", "30"))
FINGERPRINT_INTERVAL_MINUTES = int(os.getenv("FINGERPRINT_INTERVAL_MINUTES", "60"))
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "20"))

RPC_TIMEOUT_SECONDS = int(os.getenv("RPC_TIMEOUT_SECONDS", "30"))
TELEGRAM_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "20"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
RETRY_BASE_SECONDS = int(os.getenv("RETRY_BASE_SECONDS", "3"))

# Alert thresholds. They can be overridden in GitHub repository variables.
MIN_VOLUME_FOR_RATIO_USDT = Decimal(
    os.getenv("MIN_VOLUME_FOR_RATIO_USDT", "20000")
)
SELL_BUY_YELLOW = Decimal(os.getenv("SELL_BUY_YELLOW", "1.20"))
SELL_BUY_RED = Decimal(os.getenv("SELL_BUY_RED", "1.50"))
PROTOCOL_BUY_SHARE_YELLOW = Decimal(
    os.getenv("PROTOCOL_BUY_SHARE_YELLOW", "0.50")
)
RBS_24H_DROP_YELLOW = Decimal(os.getenv("RBS_24H_DROP_YELLOW", "0.10"))
RBS_24H_DROP_RED = Decimal(os.getenv("RBS_24H_DROP_RED", "0.20"))
LP_24H_DROP_YELLOW = Decimal(os.getenv("LP_24H_DROP_YELLOW", "0.05"))
LP_7D_DROP_RED = Decimal(os.getenv("LP_7D_DROP_RED", "0.10"))
NET_INFLATION_7D_YELLOW = Decimal(
    os.getenv("NET_INFLATION_7D_YELLOW", "0.05")
)
NET_INFLATION_7D_RED = Decimal(
    os.getenv("NET_INFLATION_7D_RED", "0.10")
)
RBS_LP_RATIO_YELLOW = Decimal(os.getenv("RBS_LP_RATIO_YELLOW", "0.20"))
UNKNOWN_TREASURY_OUTFLOW_USDT = Decimal(
    os.getenv("UNKNOWN_TREASURY_OUTFLOW_USDT", "25000")
)
LARGE_TREASURY_OUTFLOW_USDT = Decimal(
    os.getenv("LARGE_TREASURY_OUTFLOW_USDT", "100000")
)
ALERT_COOLDOWN_HOURS = int(os.getenv("ALERT_COOLDOWN_HOURS", "6"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
BSC_RPC = os.getenv("BSC_RPC", "").strip()


# ---------------------------------------------------------------------------
# Minimal ABIs and event topics
# ---------------------------------------------------------------------------

ERC20_ABI = [
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

PAIR_ABI = [
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token1",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getReserves",
        "outputs": [
            {"internalType": "uint112", "name": "reserve0", "type": "uint112"},
            {"internalType": "uint112", "name": "reserve1", "type": "uint112"},
            {"internalType": "uint32", "name": "blockTimestampLast", "type": "uint32"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

TRANSFER_TOPIC = Web3.to_hex(Web3.keccak(text="Transfer(address,address,uint256)")).lower()
SWAP_TOPIC = Web3.to_hex(
    Web3.keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)")
).lower()

# keccak256("eip1967.proxy.implementation") - 1
EIP1967_IMPLEMENTATION_SLOT = int(
    "360894A13BA1A3210667C828492DB98DCA3E2076CC3735A920A3CA505D382BBC", 16
)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TokenMeta:
    decimals: int
    symbol: str


@dataclass(frozen=True)
class PairMeta:
    token0: str
    token1: str
    ibs_is_token0: bool
    usdt_is_token0: bool


@dataclass
class RiskItem:
    code: str
    severity: int  # 1 yellow, 2 red, 3 black
    points: int
    title: str
    detail: str


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def require_env(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(f"缺少 GitHub Secret：{name}")
    return value


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def decimal_from(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def token_amount(raw: int, decimals: int) -> Decimal:
    return Decimal(raw) / (Decimal(10) ** decimals)


def fmt_number(value: Decimal | float | int | None, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    amount = decimal_from(value)
    if abs(amount) >= Decimal("1000000000"):
        return f"{amount / Decimal('1000000000'):.2f}B"
    if abs(amount) >= Decimal("1000000"):
        return f"{amount / Decimal('1000000'):.2f}M"
    if abs(amount) >= Decimal("1000"):
        return f"{amount / Decimal('1000'):.2f}K"
    text = f"{amount:.{digits}f}"
    return text.rstrip("0").rstrip(".")


def fmt_usdt(value: Decimal | None) -> str:
    return "N/A" if value is None else f"${fmt_number(value)}"


def fmt_pct(value: Decimal | None, signed: bool = False) -> str:
    if value is None:
        return "N/A"
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{value * 100:.2f}%"


def short_address(address: str) -> str:
    return f"{address[:8]}…{address[-6:]}"


def address_label(address: str) -> str:
    return ADDRESS_LABELS.get(address.lower(), short_address(address))


def topic_to_address(topic: Any) -> str:
    topic_hex = Web3.to_hex(topic)
    return Web3.to_checksum_address("0x" + topic_hex[-40:])


def address_to_topic(address: str) -> str:
    return "0x" + address.lower().replace("0x", "").rjust(64, "0")


def data_to_int(data: Any) -> int:
    if isinstance(data, bytes):
        return int.from_bytes(data, byteorder="big")
    return int(Web3.to_hex(data), 16)


def split_words(data: Any) -> list[int]:
    raw = bytes(data) if isinstance(data, (bytes, bytearray)) else bytes.fromhex(
        Web3.to_hex(data)[2:]
    )
    if len(raw) % 32 != 0:
        raise ValueError(f"ABI data length is not a multiple of 32: {len(raw)}")
    return [int.from_bytes(raw[i : i + 32], "big") for i in range(0, len(raw), 32)]


def event_id(log: Any) -> str:
    return f"{Web3.to_hex(log['transactionHash'])}:{int(log.get('logIndex', 0))}"


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def local_date_from_timestamp(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone(LOCAL_TZ).date().isoformat()


def safe_ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def change_ratio(current: Decimal, previous: Decimal | None) -> Decimal | None:
    if previous is None or previous <= 0:
        return None
    return (current - previous) / previous


# ---------------------------------------------------------------------------
# RPC and Telegram
# ---------------------------------------------------------------------------

def retry_call(label: str, func: Callable[[], Any]) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            print(
                f"{label}失败（{attempt}/{MAX_RETRIES}）：{type(exc).__name__}: {exc}",
                flush=True,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BASE_SECONDS * attempt)
    raise RuntimeError(f"{label}连续失败：{last_error}")


def connect_web3() -> Web3:
    rpc = require_env("BSC_RPC", BSC_RPC)
    web3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": RPC_TIMEOUT_SECONDS}))
    if not retry_call("连接 BSC RPC", web3.is_connected):
        raise RuntimeError("BSC RPC 连接失败")
    chain_id = int(retry_call("读取 Chain ID", lambda: web3.eth.chain_id))
    if chain_id != 56:
        raise RuntimeError(f"网络错误：当前 Chain ID={chain_id}，BSC Mainnet 应为 56")
    return web3


def send_telegram(message: str) -> None:
    token = require_env("BOT_TOKEN", BOT_TOKEN)
    chat_id = require_env("CHAT_ID", CHAT_ID)
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def do_send() -> None:
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
                f"HTTP {response.status_code}: {result.get('description', '未知错误')}"
            )

    retry_call("发送 Telegram", do_send)
    print("Telegram 消息发送成功", flush=True)


# ---------------------------------------------------------------------------
# State handling
# ---------------------------------------------------------------------------

def empty_daily_bucket() -> dict[str, str]:
    fields = (
        "buy_usdt",
        "sell_usdt",
        "buy_ibs",
        "sell_ibs",
        "protocol_buy_usdt",
        "mint_ibs",
        "burn_ibs",
        "rbs_usdt_in",
        "rbs_usdt_out",
        "safety_usdt_in",
        "safety_usdt_out",
        "turbine_usdt_in",
        "turbine_usdt_out",
        "bonding_usdt_in",
        "bonding_usdt_out",
    )
    return {field: "0" for field in fields}


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "last_block": 0,
        "created_at": iso_now(),
        "updated_at": iso_now(),
        "daily": {},
        "snapshots": [],
        "contract_fingerprints": {},
        "fingerprint_checked_at": None,
        "last_risk_alert": {},
        "last_daily_report_date": None,
        "seen_critical_events": [],
    }


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return default_state()
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"状态文件读取失败：{exc}") from exc
    if not isinstance(state, dict):
        raise RuntimeError("状态文件格式错误：根节点不是对象")
    merged = default_state()
    merged.update(state)
    return merged


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = iso_now()
    prune_state(state)
    atomic_write_json(STATE_FILE, state)


def prune_state(state: dict[str, Any]) -> None:
    cutoff_date = (now_utc().astimezone(LOCAL_TZ).date() - timedelta(days=40)).isoformat()
    state["daily"] = {
        date: values
        for date, values in state.get("daily", {}).items()
        if date >= cutoff_date
    }

    cutoff_time = now_utc() - timedelta(days=10)
    state["snapshots"] = [
        snap
        for snap in state.get("snapshots", [])
        if (parse_iso(snap.get("timestamp")) or now_utc()) >= cutoff_time
    ][-600:]

    state["seen_critical_events"] = state.get("seen_critical_events", [])[-500:]


def add_daily(state: dict[str, Any], date: str, field: str, amount: Decimal) -> None:
    daily = state.setdefault("daily", {})
    bucket = daily.setdefault(date, empty_daily_bucket())
    bucket[field] = str(decimal_from(bucket.get(field)) + amount)


def daily_value(state: dict[str, Any], date: str, field: str) -> Decimal:
    return decimal_from(state.get("daily", {}).get(date, {}).get(field, "0"))


# ---------------------------------------------------------------------------
# Contract metadata / snapshots
# ---------------------------------------------------------------------------

def get_token_meta(contract: Any) -> TokenMeta:
    decimals = int(retry_call("读取代币 decimals", lambda: contract.functions.decimals().call()))
    try:
        symbol = str(retry_call("读取代币 symbol", lambda: contract.functions.symbol().call()))
    except Exception:  # noqa: BLE001
        symbol = "TOKEN"
    return TokenMeta(decimals=decimals, symbol=symbol)


def get_pair_meta(pair_contract: Any) -> PairMeta:
    token0 = Web3.to_checksum_address(
        retry_call("读取 Pair token0", lambda: pair_contract.functions.token0().call())
    )
    token1 = Web3.to_checksum_address(
        retry_call("读取 Pair token1", lambda: pair_contract.functions.token1().call())
    )
    if IBS_ADDRESS not in (token0, token1) or USDT_ADDRESS not in (token0, token1):
        raise RuntimeError(f"Pair 代币不匹配：token0={token0}, token1={token1}")
    return PairMeta(
        token0=token0,
        token1=token1,
        ibs_is_token0=token0.lower() == IBS_ADDRESS.lower(),
        usdt_is_token0=token0.lower() == USDT_ADDRESS.lower(),
    )


def balance_of(contract: Any, address: str, decimals: int) -> Decimal:
    raw = int(retry_call(
        f"读取 {address_label(address)} 余额",
        lambda: contract.functions.balanceOf(address).call(),
    ))
    return token_amount(raw, decimals)


def build_snapshot(
    web3: Web3,
    block_number: int,
    ibs_contract: Any,
    usdt_contract: Any,
    pair_contract: Any,
    ibs_meta: TokenMeta,
    usdt_meta: TokenMeta,
    pair_meta: PairMeta,
) -> dict[str, Any]:
    reserves = retry_call("读取 Pair reserves", lambda: pair_contract.functions.getReserves().call())
    reserve0 = int(reserves[0])
    reserve1 = int(reserves[1])

    if pair_meta.ibs_is_token0:
        ibs_reserve = token_amount(reserve0, ibs_meta.decimals)
        usdt_reserve = token_amount(reserve1, usdt_meta.decimals)
    else:
        ibs_reserve = token_amount(reserve1, ibs_meta.decimals)
        usdt_reserve = token_amount(reserve0, usdt_meta.decimals)

    price = safe_ratio(usdt_reserve, ibs_reserve) or Decimal("0")
    total_supply_raw = int(retry_call(
        "读取 IBS totalSupply", lambda: ibs_contract.functions.totalSupply().call()
    ))
    total_supply = token_amount(total_supply_raw, ibs_meta.decimals)

    rbs_usdt = balance_of(usdt_contract, RBS_ADDRESS, usdt_meta.decimals)
    safety_usdt = balance_of(usdt_contract, SAFETY_ADDRESS, usdt_meta.decimals)
    turbine_usdt = balance_of(usdt_contract, TURBINE_ADDRESS, usdt_meta.decimals)
    bonding_usdt = balance_of(usdt_contract, BONDING_ADDRESS, usdt_meta.decimals)
    rbs_ibs = balance_of(ibs_contract, RBS_ADDRESS, ibs_meta.decimals)

    # This is explicitly a proxy, not the protocol's exact internal B_IBS formula.
    lp_backing_proxy = (
        (Decimal("2") * usdt_reserve / total_supply)
        if total_supply > 0
        else Decimal("0")
    )
    lower_band_proxy = lp_backing_proxy * Decimal("0.95")
    price_to_backing = safe_ratio(price, lp_backing_proxy)
    rbs_to_lp_usdt = safe_ratio(rbs_usdt, usdt_reserve)

    return {
        "timestamp": iso_now(),
        "block": block_number,
        "ibs_price_usdt": str(price),
        "pair_ibs_reserve": str(ibs_reserve),
        "pair_usdt_reserve": str(usdt_reserve),
        "ibs_total_supply": str(total_supply),
        "rbs_usdt": str(rbs_usdt),
        "rbs_ibs": str(rbs_ibs),
        "safety_usdt": str(safety_usdt),
        "turbine_usdt": str(turbine_usdt),
        "bonding_usdt": str(bonding_usdt),
        "lp_backing_proxy": str(lp_backing_proxy),
        "lower_band_proxy": str(lower_band_proxy),
        "price_to_backing": None if price_to_backing is None else str(price_to_backing),
        "rbs_to_lp_usdt": None if rbs_to_lp_usdt is None else str(rbs_to_lp_usdt),
    }


def should_store_snapshot(state: dict[str, Any], current: dict[str, Any]) -> bool:
    snapshots = state.get("snapshots", [])
    if not snapshots:
        return True
    previous_time = parse_iso(snapshots[-1].get("timestamp"))
    current_time = parse_iso(current.get("timestamp"))
    if previous_time is None or current_time is None:
        return True
    return current_time - previous_time >= timedelta(minutes=SNAPSHOT_INTERVAL_MINUTES)


def find_snapshot_ago(
    snapshots: list[dict[str, Any]],
    hours: int,
) -> dict[str, Any] | None:
    target = now_utc() - timedelta(hours=hours)
    eligible = []
    for snap in snapshots:
        timestamp = parse_iso(snap.get("timestamp"))
        if timestamp and timestamp <= target:
            eligible.append((timestamp, snap))
    if not eligible:
        return None
    return max(eligible, key=lambda item: item[0])[1]


# ---------------------------------------------------------------------------
# Contract fingerprint monitoring
# ---------------------------------------------------------------------------

def bytes_to_address(data: bytes | bytearray | str | None) -> str | None:
    if data is None:
        return None
    raw = bytes.fromhex(data[2:]) if isinstance(data, str) and data.startswith("0x") else bytes(data)
    if len(raw) < 20 or int.from_bytes(raw, "big") == 0:
        return None
    return Web3.to_checksum_address("0x" + raw[-20:].hex())


def call_address_function(web3: Web3, address: str, signature: str) -> str | None:
    selector = Web3.keccak(text=signature)[:4]
    try:
        result = web3.eth.call({"to": address, "data": selector})
    except Exception:  # noqa: BLE001
        return None
    return bytes_to_address(result)


def contract_fingerprint(web3: Web3, address: str) -> dict[str, Any]:
    code = bytes(retry_call(
        f"读取 {address_label(address)} bytecode", lambda: web3.eth.get_code(address)
    ))
    code_hash = hashlib.sha256(code).hexdigest()

    implementation = None
    try:
        slot = retry_call(
            f"读取 {address_label(address)} implementation slot",
            lambda: web3.eth.get_storage_at(address, EIP1967_IMPLEMENTATION_SLOT),
        )
        implementation = bytes_to_address(slot)
    except Exception:  # noqa: BLE001
        implementation = None

    if implementation is None:
        implementation = call_address_function(web3, address, "implementation()")

    owner = call_address_function(web3, address, "owner()")
    if owner is None:
        owner = call_address_function(web3, address, "getOwner()")

    return {
        "code_hash": code_hash,
        "code_size": len(code),
        "implementation": implementation,
        "owner": owner,
    }


def check_contract_changes(
    web3: Web3,
    state: dict[str, Any],
) -> list[RiskItem]:
    previous_checked = parse_iso(state.get("fingerprint_checked_at"))
    if (
        state.get("contract_fingerprints")
        and previous_checked is not None
        and now_utc() - previous_checked
        < timedelta(minutes=FINGERPRINT_INTERVAL_MINUTES)
    ):
        print("合约指纹未到检查时间，本次跳过", flush=True)
        return []

    previous_all = state.setdefault("contract_fingerprints", {})
    current_all: dict[str, Any] = {}
    risks: list[RiskItem] = []

    for label, address in PROTOCOL_CONTRACTS.items():
        current = contract_fingerprint(web3, address)
        current_all[address.lower()] = {"label": label, **current}
        previous = previous_all.get(address.lower())
        if not previous:
            continue

        if previous.get("code_hash") != current.get("code_hash"):
            risks.append(
                RiskItem(
                    code=f"code_changed:{address.lower()}",
                    severity=3,
                    points=100,
                    title=f"{label} 合约字节码变化",
                    detail="合约代码哈希与上次不同，必须人工核实后再继续参与。",
                )
            )

        old_impl = previous.get("implementation")
        new_impl = current.get("implementation")
        if old_impl != new_impl and (old_impl or new_impl):
            risks.append(
                RiskItem(
                    code=f"implementation_changed:{address.lower()}",
                    severity=3,
                    points=100,
                    title=f"{label} 实现合约变化",
                    detail=f"{old_impl or '空'} → {new_impl or '空'}",
                )
            )

        old_owner = previous.get("owner")
        new_owner = current.get("owner")
        if old_owner != new_owner and (old_owner or new_owner):
            risks.append(
                RiskItem(
                    code=f"owner_changed:{address.lower()}",
                    severity=3,
                    points=100,
                    title=f"{label} Owner 变化",
                    detail=f"{old_owner or '空'} → {new_owner or '空'}",
                )
            )

    state["contract_fingerprints"] = current_all
    state["fingerprint_checked_at"] = iso_now()
    return risks


# ---------------------------------------------------------------------------
# Log querying and parsing
# ---------------------------------------------------------------------------

def is_retryable_log_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        keyword in text
        for keyword in (
            "429",
            "rate limit",
            "too many requests",
            "block range",
            "response size",
            "query returned more than",
            "-32005",
            "timeout",
        )
    )


def get_logs_resilient(
    web3: Web3,
    params: dict[str, Any],
    from_block: int,
    to_block: int,
    label: str,
) -> list[Any]:
    if from_block > to_block:
        return []

    query = dict(params)
    query["fromBlock"] = from_block
    query["toBlock"] = to_block

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return list(web3.eth.get_logs(query))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            print(
                f"{label}日志查询失败 {from_block}-{to_block} "
                f"（{attempt}/{MAX_RETRIES}）：{exc}",
                flush=True,
            )
            if from_block < to_block and is_retryable_log_error(exc):
                break
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BASE_SECONDS * attempt)

    if from_block >= to_block:
        raise RuntimeError(f"{label}单区块日志查询失败：{last_error}")

    middle = (from_block + to_block) // 2
    return get_logs_resilient(web3, params, from_block, middle, label) + get_logs_resilient(
        web3, params, middle + 1, to_block, label
    )


def get_pair_swaps(web3: Web3, from_block: int, to_block: int) -> list[Any]:
    return get_logs_resilient(
        web3,
        {"address": PAIR_ADDRESS, "topics": [SWAP_TOPIC]},
        from_block,
        to_block,
        "Swap",
    )


def get_ibs_transfers(web3: Web3, from_block: int, to_block: int) -> list[Any]:
    return get_logs_resilient(
        web3,
        {"address": IBS_ADDRESS, "topics": [TRANSFER_TOPIC]},
        from_block,
        to_block,
        "IBS Transfer",
    )


def get_usdt_treasury_transfers(web3: Web3, from_block: int, to_block: int) -> list[Any]:
    watched = [
        address_to_topic(RBS_ADDRESS),
        address_to_topic(SAFETY_ADDRESS),
        address_to_topic(TURBINE_ADDRESS),
        address_to_topic(BONDING_ADDRESS),
    ]
    outgoing = get_logs_resilient(
        web3,
        {"address": USDT_ADDRESS, "topics": [TRANSFER_TOPIC, watched]},
        from_block,
        to_block,
        "USDT Treasury Outgoing",
    )
    incoming = get_logs_resilient(
        web3,
        {"address": USDT_ADDRESS, "topics": [TRANSFER_TOPIC, None, watched]},
        from_block,
        to_block,
        "USDT Treasury Incoming",
    )
    unique: dict[str, Any] = {}
    for log in outgoing + incoming:
        unique[event_id(log)] = log
    return list(unique.values())


def get_block_timestamp(web3: Web3, block_number: int, cache: dict[int, int]) -> int:
    if block_number not in cache:
        block = retry_call(
            f"读取区块 {block_number}", lambda: web3.eth.get_block(block_number)
        )
        cache[block_number] = int(block["timestamp"])
    return cache[block_number]


def parse_swap(log: Any, pair_meta: PairMeta, ibs_meta: TokenMeta, usdt_meta: TokenMeta) -> dict[str, Any]:
    words = split_words(log["data"])
    if len(words) < 4:
        raise ValueError("Swap event data fields less than 4")
    amount0_in, amount1_in, amount0_out, amount1_out = words[:4]

    if pair_meta.ibs_is_token0:
        raw_ibs_in, raw_ibs_out = amount0_in, amount0_out
        raw_usdt_in, raw_usdt_out = amount1_in, amount1_out
    else:
        raw_ibs_in, raw_ibs_out = amount1_in, amount1_out
        raw_usdt_in, raw_usdt_out = amount0_in, amount0_out

    if raw_ibs_in > 0 and raw_usdt_out > 0:
        side = "SELL"
        ibs_amount = token_amount(raw_ibs_in, ibs_meta.decimals)
        usdt_amount = token_amount(raw_usdt_out, usdt_meta.decimals)
    elif raw_ibs_out > 0 and raw_usdt_in > 0:
        side = "BUY"
        ibs_amount = token_amount(raw_ibs_out, ibs_meta.decimals)
        usdt_amount = token_amount(raw_usdt_in, usdt_meta.decimals)
    else:
        side = "OTHER"
        ibs_amount = Decimal("0")
        usdt_amount = Decimal("0")

    sender = topic_to_address(log["topics"][1]) if len(log["topics"]) > 1 else ZERO_ADDRESS
    recipient = topic_to_address(log["topics"][2]) if len(log["topics"]) > 2 else ZERO_ADDRESS
    return {
        "side": side,
        "ibs": ibs_amount,
        "usdt": usdt_amount,
        "sender": sender,
        "recipient": recipient,
        "tx_hash": Web3.to_hex(log["transactionHash"]),
        "block": int(log["blockNumber"]),
    }


def is_protocol_swap(
    web3: Web3,
    swap: dict[str, Any],
    tx_sender_cache: dict[str, str],
) -> bool:
    if swap["sender"].lower() in PROTOCOL_ADDRESS_SET:
        return True
    if swap["recipient"].lower() in PROTOCOL_ADDRESS_SET:
        return True

    tx_hash = swap["tx_hash"]
    if tx_hash not in tx_sender_cache:
        try:
            tx = web3.eth.get_transaction(tx_hash)
            tx_sender_cache[tx_hash] = Web3.to_checksum_address(tx["from"])
        except Exception:  # noqa: BLE001
            tx_sender_cache[tx_hash] = ZERO_ADDRESS
    return tx_sender_cache[tx_hash].lower() in PROTOCOL_ADDRESS_SET


def process_scanned_logs(
    web3: Web3,
    state: dict[str, Any],
    swaps: Iterable[Any],
    ibs_transfers: Iterable[Any],
    usdt_transfers: Iterable[Any],
    pair_meta: PairMeta,
    ibs_meta: TokenMeta,
    usdt_meta: TokenMeta,
) -> list[RiskItem]:
    block_timestamp_cache: dict[int, int] = {}
    tx_sender_cache: dict[str, str] = {}
    risks: list[RiskItem] = []
    seen_critical = set(state.get("seen_critical_events", []))

    for log in swaps:
        swap = parse_swap(log, pair_meta, ibs_meta, usdt_meta)
        if swap["side"] == "OTHER":
            continue
        timestamp = get_block_timestamp(web3, swap["block"], block_timestamp_cache)
        date = local_date_from_timestamp(timestamp)
        if swap["side"] == "BUY":
            add_daily(state, date, "buy_usdt", swap["usdt"])
            add_daily(state, date, "buy_ibs", swap["ibs"])
            if is_protocol_swap(web3, swap, tx_sender_cache):
                add_daily(state, date, "protocol_buy_usdt", swap["usdt"])
        else:
            add_daily(state, date, "sell_usdt", swap["usdt"])
            add_daily(state, date, "sell_ibs", swap["ibs"])

    for log in ibs_transfers:
        if len(log["topics"]) < 3:
            continue
        source = topic_to_address(log["topics"][1])
        target = topic_to_address(log["topics"][2])
        amount = token_amount(data_to_int(log["data"]), ibs_meta.decimals)
        block_number = int(log["blockNumber"])
        timestamp = get_block_timestamp(web3, block_number, block_timestamp_cache)
        date = local_date_from_timestamp(timestamp)
        if source.lower() == ZERO_ADDRESS.lower():
            add_daily(state, date, "mint_ibs", amount)
        if target.lower() in {ZERO_ADDRESS.lower(), DEAD_ADDRESS.lower()}:
            add_daily(state, date, "burn_ibs", amount)

    watched_fields = {
        RBS_ADDRESS.lower(): ("rbs_usdt_in", "rbs_usdt_out", "RBS Stabilizer"),
        SAFETY_ADDRESS.lower(): ("safety_usdt_in", "safety_usdt_out", "Safety Treasury"),
        TURBINE_ADDRESS.lower(): ("turbine_usdt_in", "turbine_usdt_out", "Release Turbine"),
        BONDING_ADDRESS.lower(): ("bonding_usdt_in", "bonding_usdt_out", "Bonding"),
    }

    for log in usdt_transfers:
        if len(log["topics"]) < 3:
            continue
        source = topic_to_address(log["topics"][1])
        target = topic_to_address(log["topics"][2])
        amount = token_amount(data_to_int(log["data"]), usdt_meta.decimals)
        block_number = int(log["blockNumber"])
        timestamp = get_block_timestamp(web3, block_number, block_timestamp_cache)
        date = local_date_from_timestamp(timestamp)

        if target.lower() in watched_fields:
            in_field, _, _ = watched_fields[target.lower()]
            add_daily(state, date, in_field, amount)
        if source.lower() in watched_fields:
            _, out_field, source_label = watched_fields[source.lower()]
            add_daily(state, date, out_field, amount)

            eid = event_id(log)
            is_unknown = target.lower() not in KNOWN_SAFE_RECIPIENTS
            if (
                source.lower() in {RBS_ADDRESS.lower(), SAFETY_ADDRESS.lower()}
                and amount >= UNKNOWN_TREASURY_OUTFLOW_USDT
                and is_unknown
                and eid not in seen_critical
            ):
                severity = 3 if amount >= LARGE_TREASURY_OUTFLOW_USDT else 2
                risks.append(
                    RiskItem(
                        code=f"unknown_outflow:{eid}",
                        severity=severity,
                        points=100 if severity == 3 else 45,
                        title=f"{source_label} 向陌生地址转出 USDT",
                        detail=(
                            f"{fmt_usdt(amount)} → {html.escape(target)}\n"
                            f"<a href=\"https://bscscan.com/tx/{Web3.to_hex(log['transactionHash'])}\">查看交易</a>"
                        ),
                    )
                )
                seen_critical.add(eid)

    state["seen_critical_events"] = list(seen_critical)
    return risks


# ---------------------------------------------------------------------------
# Risk calculations
# ---------------------------------------------------------------------------

def sum_daily(
    state: dict[str, Any],
    dates: list[str],
    field: str,
) -> Decimal:
    return sum((daily_value(state, date, field) for date in dates), Decimal("0"))


def recent_dates(days: int) -> list[str]:
    today = now_utc().astimezone(LOCAL_TZ).date()
    return [(today - timedelta(days=offset)).isoformat() for offset in range(days)]


def estimate_support_days(
    state: dict[str, Any],
    current_rbs: Decimal,
    old_24h: dict[str, Any] | None,
) -> Decimal | None:
    rates: list[Decimal] = []

    dates = recent_dates(7)
    net_out_7d = Decimal("0")
    observed_days = 0
    for date in dates:
        if date in state.get("daily", {}):
            observed_days += 1
            outflow = daily_value(state, date, "rbs_usdt_out")
            inflow = daily_value(state, date, "rbs_usdt_in")
            net_out_7d += max(outflow - inflow, Decimal("0"))
    if observed_days > 0 and net_out_7d > 0:
        rates.append(net_out_7d / Decimal(observed_days))

    if old_24h:
        old_rbs = decimal_from(old_24h.get("rbs_usdt"))
        old_time = parse_iso(old_24h.get("timestamp"))
        elapsed_days = (
            Decimal(str((now_utc() - old_time).total_seconds())) / Decimal("86400")
            if old_time
            else Decimal("0")
        )
        depletion = old_rbs - current_rbs
        if elapsed_days > 0 and depletion > 0:
            rates.append(depletion / elapsed_days)

    if not rates:
        return None
    conservative_daily_depletion = max(rates)
    if conservative_daily_depletion <= 0:
        return None
    return current_rbs / conservative_daily_depletion


def calculate_risks(
    state: dict[str, Any],
    current: dict[str, Any],
    event_risks: list[RiskItem],
    contract_risks: list[RiskItem],
) -> tuple[list[RiskItem], dict[str, Any]]:
    risks = list(event_risks) + list(contract_risks)
    today = recent_dates(1)[0]
    dates_7d = recent_dates(7)

    buy_today = daily_value(state, today, "buy_usdt")
    sell_today = daily_value(state, today, "sell_usdt")
    protocol_buy_today = daily_value(state, today, "protocol_buy_usdt")
    external_buy_today = max(buy_today - protocol_buy_today, Decimal("0"))
    sell_buy_ratio = safe_ratio(sell_today, external_buy_today)
    protocol_buy_share = safe_ratio(protocol_buy_today, buy_today)

    mint_7d = sum_daily(state, dates_7d, "mint_ibs")
    burn_7d = sum_daily(state, dates_7d, "burn_ibs")
    net_mint_7d = mint_7d - burn_7d

    total_supply = decimal_from(current["ibs_total_supply"])
    net_inflation_7d = safe_ratio(net_mint_7d, total_supply)
    current_rbs = decimal_from(current["rbs_usdt"])
    current_lp_usdt = decimal_from(current["pair_usdt_reserve"])
    rbs_to_lp = safe_ratio(current_rbs, current_lp_usdt)

    snapshots = state.get("snapshots", [])
    old_24h = find_snapshot_ago(snapshots, 24)
    old_7d = find_snapshot_ago(snapshots, 24 * 7)

    rbs_24h_change = change_ratio(
        current_rbs,
        decimal_from(old_24h.get("rbs_usdt")) if old_24h else None,
    )
    lp_24h_change = change_ratio(
        current_lp_usdt,
        decimal_from(old_24h.get("pair_usdt_reserve")) if old_24h else None,
    )
    lp_7d_change = change_ratio(
        current_lp_usdt,
        decimal_from(old_7d.get("pair_usdt_reserve")) if old_7d else None,
    )
    support_days = estimate_support_days(state, current_rbs, old_24h)

    price = decimal_from(current["ibs_price_usdt"])
    backing_proxy = decimal_from(current["lp_backing_proxy"])
    price_to_backing = safe_ratio(price, backing_proxy)

    if sell_today >= MIN_VOLUME_FOR_RATIO_USDT and external_buy_today < Decimal("1000"):
        risks.append(
            RiskItem(
                code="sell_without_external_buy",
                severity=2,
                points=45,
                title="今日有明显卖出，但几乎没有外部买盘",
                detail=(
                    f"卖出 {fmt_usdt(sell_today)}，外部买入仅 "
                    f"{fmt_usdt(external_buy_today)}"
                ),
            )
        )
    elif (
        sell_buy_ratio is not None
        and sell_today >= MIN_VOLUME_FOR_RATIO_USDT
        and external_buy_today >= Decimal("1000")
    ):
        if sell_buy_ratio >= SELL_BUY_RED:
            risks.append(
                RiskItem(
                    code="sell_buy_red",
                    severity=2,
                    points=35,
                    title="今日真实抛压明显高于外部买盘",
                    detail=(
                        f"卖出/外部买入={sell_buy_ratio:.2f}，"
                        f"卖出 {fmt_usdt(sell_today)}，外部买入 {fmt_usdt(external_buy_today)}"
                    ),
                )
            )
        elif sell_buy_ratio >= SELL_BUY_YELLOW:
            risks.append(
                RiskItem(
                    code="sell_buy_yellow",
                    severity=1,
                    points=20,
                    title="今日抛压开始超过外部买盘",
                    detail=f"卖出/外部买入={sell_buy_ratio:.2f}",
                )
            )

    if (
        protocol_buy_share is not None
        and buy_today >= MIN_VOLUME_FOR_RATIO_USDT
        and protocol_buy_share >= PROTOCOL_BUY_SHARE_YELLOW
    ):
        risks.append(
            RiskItem(
                code="protocol_buy_share_high",
                severity=1,
                points=20,
                title="买盘对协议资金依赖较高",
                detail=(
                    f"可识别协议买盘占比 {fmt_pct(protocol_buy_share)}。"
                    "该比例是保守估计，未识别出的协议路由交易可能使真实占比更高。"
                ),
            )
        )

    if rbs_24h_change is not None:
        rbs_drop = -rbs_24h_change
        if rbs_drop >= RBS_24H_DROP_RED:
            risks.append(
                RiskItem(
                    code="rbs_24h_drop_red",
                    severity=2,
                    points=45,
                    title="RBS 24小时快速失血",
                    detail=f"RBS USDT 约下降 {fmt_pct(rbs_drop)}",
                )
            )
        elif rbs_drop >= RBS_24H_DROP_YELLOW:
            risks.append(
                RiskItem(
                    code="rbs_24h_drop_yellow",
                    severity=1,
                    points=25,
                    title="RBS 24小时下降超过预警线",
                    detail=f"RBS USDT 约下降 {fmt_pct(rbs_drop)}",
                )
            )

    if lp_24h_change is not None and -lp_24h_change >= LP_24H_DROP_YELLOW:
        risks.append(
            RiskItem(
                code="lp_24h_drop",
                severity=1,
                points=25,
                title="LP池 USDT 24小时明显下降",
                detail=f"下降约 {fmt_pct(-lp_24h_change)}",
            )
        )

    if lp_7d_change is not None and -lp_7d_change >= LP_7D_DROP_RED:
        risks.append(
            RiskItem(
                code="lp_7d_drop_red",
                severity=2,
                points=40,
                title="LP池 USDT 7日下降超过红线",
                detail=f"下降约 {fmt_pct(-lp_7d_change)}",
            )
        )

    if net_inflation_7d is not None:
        if net_inflation_7d >= NET_INFLATION_7D_RED:
            risks.append(
                RiskItem(
                    code="inflation_7d_red",
                    severity=2,
                    points=35,
                    title="IBS 7日净通胀超过红线",
                    detail=(
                        f"净增发 {fmt_number(net_mint_7d)} IBS，"
                        f"约为当前供应量 {fmt_pct(net_inflation_7d)}"
                    ),
                )
            )
        elif net_inflation_7d >= NET_INFLATION_7D_YELLOW:
            risks.append(
                RiskItem(
                    code="inflation_7d_yellow",
                    severity=1,
                    points=20,
                    title="IBS 7日净通胀偏高",
                    detail=f"约为当前供应量 {fmt_pct(net_inflation_7d)}",
                )
            )

    if rbs_to_lp is not None and rbs_to_lp < RBS_LP_RATIO_YELLOW:
        risks.append(
            RiskItem(
                code="rbs_lp_ratio_low",
                severity=1,
                points=20,
                title="RBS/LP USDT 比例偏低",
                detail=(
                    f"代理比率 {fmt_pct(rbs_to_lp)}。"
                    "这是链上余额代理值，不等同于项目合约内部的精确储备率。"
                ),
            )
        )

    if support_days is not None:
        if support_days <= Decimal("7"):
            risks.append(
                RiskItem(
                    code="support_days_black",
                    severity=3,
                    points=100,
                    title="按当前消耗速度，RBS或不足7天",
                    detail=f"保守估算约 {support_days:.1f} 天",
                )
            )
        elif support_days <= Decimal("14"):
            risks.append(
                RiskItem(
                    code="support_days_red",
                    severity=2,
                    points=50,
                    title="按当前消耗速度，RBS或不足14天",
                    detail=f"保守估算约 {support_days:.1f} 天",
                )
            )

    if price_to_backing is not None:
        if price_to_backing < Decimal("0.95"):
            risks.append(
                RiskItem(
                    code="price_below_proxy_band",
                    severity=2,
                    points=45,
                    title="IBS价格跌破LP支持价值代理下沿",
                    detail=(
                        f"价格 {fmt_usdt(price)}，LP支持价值代理 {fmt_usdt(backing_proxy)}。"
                        "该支持价值是近似计算，不是合约内部精确 B_IBS。"
                    ),
                )
            )
        elif price_to_backing < Decimal("1.00"):
            risks.append(
                RiskItem(
                    code="price_near_proxy_band",
                    severity=1,
                    points=20,
                    title="IBS价格接近LP支持价值代理",
                    detail=f"价格/支持价值代理={price_to_backing:.3f}",
                )
            )

    unique: dict[str, RiskItem] = {}
    for risk in risks:
        old = unique.get(risk.code)
        if old is None or risk.severity > old.severity:
            unique[risk.code] = risk
    risks = sorted(unique.values(), key=lambda item: (-item.severity, -item.points, item.code))

    score = min(100, sum(item.points for item in risks))
    max_severity = max((item.severity for item in risks), default=0)
    if max_severity >= 3 or score >= 75:
        level = "BLACK"
        advice = "优先撤退，不等待30天；先保证资金安全，再核实原因。"
    elif max_severity >= 2 or score >= 50:
        level = "RED"
        advice = "停止复利，优先回收本金，并考虑撤出50%—80%。"
    elif max_severity >= 1 or score >= 25:
        level = "YELLOW"
        advice = "停止新增投入，停止复利，开始持续回收本金。"
    else:
        level = "GREEN"
        advice = "暂未触发撤退线，但日化1%仍属于极高风险收益，继续只用可承受损失的资金。"

    metrics = {
        "today": today,
        "buy_today": buy_today,
        "sell_today": sell_today,
        "protocol_buy_today": protocol_buy_today,
        "external_buy_today": external_buy_today,
        "sell_buy_ratio": sell_buy_ratio,
        "protocol_buy_share": protocol_buy_share,
        "mint_7d": mint_7d,
        "burn_7d": burn_7d,
        "net_mint_7d": net_mint_7d,
        "net_inflation_7d": net_inflation_7d,
        "rbs_24h_change": rbs_24h_change,
        "lp_24h_change": lp_24h_change,
        "lp_7d_change": lp_7d_change,
        "support_days": support_days,
        "rbs_to_lp": rbs_to_lp,
        "price_to_backing": price_to_backing,
        "score": score,
        "level": level,
        "advice": advice,
    }
    return risks, metrics


# ---------------------------------------------------------------------------
# Message formatting and anti-spam
# ---------------------------------------------------------------------------

LEVEL_STYLE = {
    "GREEN": ("🟢", "绿色"),
    "YELLOW": ("🟡", "黄色"),
    "RED": ("🔴", "红色"),
    "BLACK": ("⚫", "黑色"),
}


def risk_signature(risks: list[RiskItem], level: str) -> str:
    payload = level + "|" + "|".join(sorted(risk.code for risk in risks))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def should_send_risk_alert(state: dict[str, Any], risks: list[RiskItem], metrics: dict[str, Any]) -> bool:
    previous = state.get("last_risk_alert", {})
    current_signature = risk_signature(risks, metrics["level"])
    previous_signature = previous.get("signature")
    previous_level = previous.get("level", "GREEN")
    previous_time = parse_iso(previous.get("sent_at"))

    rank = {"GREEN": 0, "YELLOW": 1, "RED": 2, "BLACK": 3}
    worsened = rank[metrics["level"]] > rank.get(previous_level, 0)
    changed = current_signature != previous_signature
    cooldown_elapsed = (
        previous_time is None
        or now_utc() - previous_time >= timedelta(hours=ALERT_COOLDOWN_HOURS)
    )
    recovered = metrics["level"] == "GREEN" and rank.get(previous_level, 0) >= 2

    if metrics["level"] == "GREEN" and not recovered:
        return False
    return worsened or changed or cooldown_elapsed or recovered


def update_alert_state(state: dict[str, Any], risks: list[RiskItem], metrics: dict[str, Any]) -> None:
    state["last_risk_alert"] = {
        "signature": risk_signature(risks, metrics["level"]),
        "level": metrics["level"],
        "sent_at": iso_now(),
    }


def format_risk_message(
    risks: list[RiskItem],
    metrics: dict[str, Any],
    current: dict[str, Any],
) -> str:
    icon, cn_level = LEVEL_STYLE[metrics["level"]]
    lines = [
        f"{icon} <b>POTS 撤退风险：{cn_level}（{metrics['score']}/100）</b>",
        "",
        f"<b>行动建议：</b>{html.escape(metrics['advice'])}",
        "",
        "<b>核心数据</b>",
        f"IBS价格：<b>{fmt_usdt(decimal_from(current['ibs_price_usdt']))}</b>",
        f"RBS USDT：<b>{fmt_usdt(decimal_from(current['rbs_usdt']))}</b>",
        f"LP池 USDT：<b>{fmt_usdt(decimal_from(current['pair_usdt_reserve']))}</b>",
        f"今日外部买入：{fmt_usdt(metrics['external_buy_today'])}",
        f"今日卖出：{fmt_usdt(metrics['sell_today'])}",
        f"卖出/外部买入：{fmt_number(metrics['sell_buy_ratio'])}",
        f"可识别协议买盘占比：{fmt_pct(metrics['protocol_buy_share'])}",
        f"7日净增发：{fmt_number(metrics['net_mint_7d'])} IBS",
        f"RBS可支撑天数：{fmt_number(metrics['support_days'], 1)}",
    ]

    if risks:
        lines.extend(["", "<b>触发原因</b>"])
        for index, risk in enumerate(risks[:8], start=1):
            severity_icon = {1: "🟡", 2: "🔴", 3: "⚫"}[risk.severity]
            lines.append(f"{index}. {severity_icon} <b>{html.escape(risk.title)}</b>")
            lines.append(html.escape(risk.detail) if "<a href=" not in risk.detail else risk.detail)
        if len(risks) > 8:
            lines.append(f"另有 {len(risks) - 8} 项风险未展开。")

    lines.extend(
        [
            "",
            f"区块：<code>{current['block']}</code>",
            '<a href="https://bscscan.com/address/0xCBA922f6aff0EC8CB0703D44249456Ef779A394C">RBS</a> ｜ '
            '<a href="https://bscscan.com/address/0x2a4b99a9c4544d35e8d266111c50b67fea01d53d">IBS/USDT池</a>',
        ]
    )
    return "\n".join(lines)


def format_daily_report(metrics: dict[str, Any], current: dict[str, Any]) -> str:
    icon, cn_level = LEVEL_STYLE[metrics["level"]]
    return "\n".join(
        [
            f"📊 <b>POTS 每日风险报告 · {metrics['today']}</b>",
            "",
            f"风险等级：{icon} <b>{cn_level}</b>（{metrics['score']}/100）",
            f"行动建议：{html.escape(metrics['advice'])}",
            "",
            "<b>储备</b>",
            f"RBS USDT：{fmt_usdt(decimal_from(current['rbs_usdt']))}",
            f"RBS 24h变化：{fmt_pct(metrics['rbs_24h_change'], signed=True)}",
            f"RBS/LP USDT代理比率：{fmt_pct(metrics['rbs_to_lp'])}",
            f"RBS可支撑天数：{fmt_number(metrics['support_days'], 1)}",
            f"Safety Treasury USDT：{fmt_usdt(decimal_from(current['safety_usdt']))}",
            "",
            "<b>流动性与价格</b>",
            f"IBS价格：{fmt_usdt(decimal_from(current['ibs_price_usdt']))}",
            f"LP池 USDT：{fmt_usdt(decimal_from(current['pair_usdt_reserve']))}",
            f"LP 24h变化：{fmt_pct(metrics['lp_24h_change'], signed=True)}",
            f"LP 7d变化：{fmt_pct(metrics['lp_7d_change'], signed=True)}",
            f"价格/LP支持价值代理：{fmt_number(metrics['price_to_backing'], 3)}",
            "",
            "<b>今日买卖压力</b>",
            f"总买入：{fmt_usdt(metrics['buy_today'])}",
            f"外部买入：{fmt_usdt(metrics['external_buy_today'])}",
            f"可识别协议买入：{fmt_usdt(metrics['protocol_buy_today'])}",
            f"卖出：{fmt_usdt(metrics['sell_today'])}",
            f"卖出/外部买入：{fmt_number(metrics['sell_buy_ratio'])}",
            "",
            "<b>7日供应</b>",
            f"Mint：{fmt_number(metrics['mint_7d'])} IBS",
            f"Burn：{fmt_number(metrics['burn_7d'])} IBS",
            f"净增发：{fmt_number(metrics['net_mint_7d'])} IBS",
            f"净通胀率：{fmt_pct(metrics['net_inflation_7d'])}",
            "",
            "说明：协议买盘归因和LP支持价值均为链上保守代理，不能替代完整合约会计。",
        ]
    )


def should_send_daily_report(state: dict[str, Any]) -> bool:
    local_now = now_utc().astimezone(LOCAL_TZ)
    today = local_now.date().isoformat()
    return (
        local_now.hour >= DAILY_REPORT_HOUR
        and state.get("last_daily_report_date") != today
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    started = time.monotonic()
    print("POTS 风险监控启动", flush=True)

    web3 = connect_web3()
    state = load_state()

    ibs_contract = web3.eth.contract(address=IBS_ADDRESS, abi=ERC20_ABI)
    usdt_contract = web3.eth.contract(address=USDT_ADDRESS, abi=ERC20_ABI)
    pair_contract = web3.eth.contract(address=PAIR_ADDRESS, abi=PAIR_ABI)

    ibs_meta = get_token_meta(ibs_contract)
    usdt_meta = get_token_meta(usdt_contract)
    pair_meta = get_pair_meta(pair_contract)

    latest = int(retry_call("读取最新区块", lambda: web3.eth.block_number))
    safe_latest = max(1, latest - CONFIRMATION_BLOCKS)

    first_run = int(state.get("last_block", 0)) <= 0
    if first_run:
        state["last_block"] = safe_latest - 1

    last_block = int(state["last_block"])
    run_target = min(safe_latest, last_block + MAX_BLOCKS_PER_RUN)

    event_risks: list[RiskItem] = []
    current_block = last_block + 1
    while current_block <= run_target:
        if time.monotonic() - started >= MAX_RUNTIME_SECONDS:
            print("接近运行时间上限，保存进度并退出", flush=True)
            break

        end_block = min(current_block + BLOCK_CHUNK_SIZE - 1, run_target)
        print(f"扫描区块 {current_block}-{end_block}", flush=True)

        swaps = get_pair_swaps(web3, current_block, end_block)
        ibs_transfers = get_ibs_transfers(web3, current_block, end_block)
        usdt_transfers = get_usdt_treasury_transfers(web3, current_block, end_block)

        print(
            f"Swap={len(swaps)}, IBS Transfer={len(ibs_transfers)}, "
            f"Treasury USDT Transfer={len(usdt_transfers)}",
            flush=True,
        )

        event_risks.extend(
            process_scanned_logs(
                web3,
                state,
                swaps,
                ibs_transfers,
                usdt_transfers,
                pair_meta,
                ibs_meta,
                usdt_meta,
            )
        )
        state["last_block"] = end_block
        save_state(state)
        current_block = end_block + 1

    completed_block = int(state["last_block"])
    snapshot = build_snapshot(
        web3,
        completed_block,
        ibs_contract,
        usdt_contract,
        pair_contract,
        ibs_meta,
        usdt_meta,
        pair_meta,
    )

    contract_risks = check_contract_changes(web3, state)

    if should_store_snapshot(state, snapshot):
        state.setdefault("snapshots", []).append(snapshot)

    risks, metrics = calculate_risks(state, snapshot, event_risks, contract_risks)

    if first_run:
        startup_message = "\n".join(
            [
                "✅ <b>POTS 撤退风险监控已启动</b>",
                "",
                f"IBS价格：{fmt_usdt(decimal_from(snapshot['ibs_price_usdt']))}",
                f"RBS USDT：{fmt_usdt(decimal_from(snapshot['rbs_usdt']))}",
                f"LP池 USDT：{fmt_usdt(decimal_from(snapshot['pair_usdt_reserve']))}",
                f"当前风险：{LEVEL_STYLE[metrics['level']][0]} {LEVEL_STYLE[metrics['level']][1]}（{metrics['score']}/100）",
                "",
                "每5分钟扫描链上变化；每天20:00（Vancouver时间）发送日报。",
                "首次运行只建立基准，不补发历史报警。",
            ]
        )
        send_telegram(startup_message)
        update_alert_state(state, risks, metrics)
    elif should_send_risk_alert(state, risks, metrics):
        send_telegram(format_risk_message(risks, metrics, snapshot))
        update_alert_state(state, risks, metrics)

    if should_send_daily_report(state):
        send_telegram(format_daily_report(metrics, snapshot))
        state["last_daily_report_date"] = now_utc().astimezone(LOCAL_TZ).date().isoformat()

    save_state(state)

    remaining = safe_latest - int(state["last_block"])
    print(f"监控完成，剩余待扫区块：{max(0, remaining)}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"程序运行失败：{type(exc).__name__}: {exc}", flush=True)
        sys.exit(1)
