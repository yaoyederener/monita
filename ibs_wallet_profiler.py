#!/usr/bin/env python3
"""Independent IBS seller wallet profiler for GitHub Actions + Telegram.

The job watches the official IBS/USDT pair for sells strictly above the configured
IBS threshold.  Every qualifying tx.from address is profiled from its complete
IBS transfer history (Alchemy Transfers API) and the Swap logs in those
transactions.  It never needs a private key and does not modify the core-funds
monitor state.
"""

from __future__ import annotations

import html
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional

import requests
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware


IBS_ADDRESS = Web3.to_checksum_address("0x255e746aBb8D9Acac00d6d023e5E63E3b8DFA7cd")
USDT_ADDRESS = Web3.to_checksum_address("0x55d398326f99059fF775485246999027B3197955")
PAIR_ADDRESS = Web3.to_checksum_address("0x2a4b99a9c4544d35e8d266111c50b67fea01d53d")
STAKING_ADDRESS = Web3.to_checksum_address("0x6025FC9840Cc4e282125a74F4b00dC5038A8058f")

PROTOCOL_ADDRESSES = {
    "IBS Token": IBS_ADDRESS,
    "IBS/USDT LP": PAIR_ADDRESS,
    "RBS Stabilizer": "0xCBA922f6aff0EC8CB0703D44249456Ef779A394C",
    "Safety Treasury": "0x5BB0d5Cb2276a054d933B14D023A2063CF8F28Ce",
    "Bonding": "0x89E6EFd26aF347fD7f1Eb9846a21E4e85311CC30",
    "Operator Bond": "0xb83d56a4de0f080d9a0ccb7B67e747af68bbC655",
    "Staking": STAKING_ADDRESS,
    "Release Turbine": "0x004202D0b1759BcDBD939BC5a2BfBCEeD9DD34b1",
    "IBS AEM": "0xE72a413864B8f795f2a1c2de4176e4BE9BF56F34",
    "Rebase Pool": "0xC274041Bf5d9487baB196E63e6609B6161FFCD7d",
    "BTCB Treasury": "0xE9A7c7Bb2D4264940296d5D6C414d09DD37627F0",
    "Worldpool Treasury": "0x7266256440a32f5dA2691B1EF98Fffb5b655658a",
    "Legacy Worldpool Treasury": "0xc407928502e0aa6D313494EC2EE224DB55DcF1FC",
    "RBS Executor": "0xf3fc289ABbfF1F847649bC738A4e39D2ED365711",
}
PROTOCOL_LABELS = {address.lower(): label for label, address in PROTOCOL_ADDRESSES.items()}

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
SWAP_TOPIC = Web3.to_hex(Web3.keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)"))
TRANSFER_TOPIC = Web3.to_hex(Web3.keccak(text="Transfer(address,address,uint256)"))
STATE_FILE = Path(os.getenv("WALLET_PROFILE_STATE_FILE", "data/ibs_wallet_profiles.json"))

CONFIRMATION_BLOCKS = int(os.getenv("CONFIRMATION_BLOCKS", "3"))
SELL_THRESHOLD_IBS = Decimal(os.getenv("SELL_THRESHOLD_IBS", "20"))
MAX_SCAN_BLOCKS = int(os.getenv("MAX_SCAN_BLOCKS", "20000"))
LOG_CHUNK_SIZE = int(os.getenv("LOG_CHUNK_SIZE", "2000"))
MAX_PROFILES_PER_RUN = int(os.getenv("MAX_PROFILES_PER_RUN", "2"))
RPC_TIMEOUT_SECONDS = int(os.getenv("RPC_TIMEOUT_SECONDS", "30"))
TELEGRAM_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "20"))
ANOMALY_MIN_TRADES = int(os.getenv("ANOMALY_MIN_TRADES", "12"))
ANOMALY_MIN_NET_OUTFLOW_USDT = Decimal(os.getenv("ANOMALY_MIN_NET_OUTFLOW_USDT", "1000"))
ANOMALY_MIN_PROTOCOL_PROCEEDS_USDT = Decimal(os.getenv("ANOMALY_MIN_PROTOCOL_PROCEEDS_USDT", "1000"))
ANOMALY_REPEAT_HOURS = int(os.getenv("ANOMALY_REPEAT_HOURS", "24"))
ANOMALY_NEW_SELLS = int(os.getenv("ANOMALY_NEW_SELLS", "10"))
LARGE_SELL_USDT = Decimal(os.getenv("LARGE_SELL_USDT", "5000"))

ERC20_ABI = [
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "treasury", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "taxTreasury", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
]
PAIR_ABI = [
    {"inputs": [], "name": "token0", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getReserves", "outputs": [{"type": "uint112"}, {"type": "uint112"}, {"type": "uint32"}], "stateMutability": "view", "type": "function"},
]


@dataclass(frozen=True)
class Trade:
    tx_hash: str
    block_number: int
    log_index: int
    side: str
    ibs_raw: int
    usdt_raw: int
    timestamp: Optional[datetime] = None
    token_source: Optional[str] = None
    quote_recipient: Optional[str] = None


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


def connect_web3(rpc_url: str) -> Web3:
    web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": RPC_TIMEOUT_SECONDS}))
    web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not web3.is_connected() or int(web3.eth.chain_id) != 56:
        raise RuntimeError("BSC RPC连接失败或网络不是BSC主网")
    return web3


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"schema_version": 1, "profiles": {}, "pending": [], "seen": []}
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    state.setdefault("profiles", {})
    state.setdefault("pending", [])
    state.setdefault("seen", [])
    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(STATE_FILE)


def compact_pending_by_address(pending: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one latest trigger per address while retaining every event id for dedupe."""
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for source in pending:
        record = dict(source)
        key = str(record.get("address", "")).lower()
        if not key:
            continue
        event_ids = list(record.get("event_ids", [record["event_id"]]))
        if key not in grouped:
            record["event_ids"] = event_ids
            grouped[key] = record
            order.append(key)
            continue
        current = grouped[key]
        current["event_ids"] = list(dict.fromkeys(list(current.get("event_ids", [])) + event_ids))
        if (int(record["block_number"]), int(record["log_index"])) > (int(current["block_number"]), int(current["log_index"])):
            retained_ids = current["event_ids"]
            current.update(record)
            current["event_ids"] = retained_ids
    return [grouped[key] for key in order]


def decode_words(data: Any) -> tuple[int, int, int, int]:
    raw = bytes(data) if not isinstance(data, str) else bytes.fromhex(data.removeprefix("0x"))
    if len(raw) != 128:
        raise RuntimeError("Swap日志data长度异常")
    return tuple(int.from_bytes(raw[i:i + 32], "big") for i in range(0, 128, 32))  # type: ignore[return-value]


def decode_trade(log: Any, ibs_is_token0: bool) -> Optional[Trade]:
    a0in, a1in, a0out, a1out = decode_words(log["data"])
    ibs_in, ibs_out = (a0in, a0out) if ibs_is_token0 else (a1in, a1out)
    usdt_in, usdt_out = (a1in, a1out) if ibs_is_token0 else (a0in, a0out)
    ibs_net, usdt_net = ibs_in - ibs_out, usdt_in - usdt_out
    if ibs_net > 0 and usdt_net < 0:
        side, ibs_raw, usdt_raw = "SELL", ibs_net, -usdt_net
    elif ibs_net < 0 and usdt_net > 0:
        side, ibs_raw, usdt_raw = "BUY", -ibs_net, usdt_net
    else:
        return None
    tx_hash = Web3.to_hex(log["transactionHash"])
    return Trade(tx_hash, int(log["blockNumber"]), int(log["logIndex"]), side, ibs_raw, usdt_raw)


def get_swap_logs(web3: Web3, from_block: int, to_block: int) -> list[Any]:
    logs: list[Any] = []
    start = from_block
    while start <= to_block:
        end = min(to_block, start + LOG_CHUNK_SIZE - 1)
        try:
            logs.extend(web3.eth.get_logs({"address": PAIR_ADDRESS, "fromBlock": start, "toBlock": end, "topics": [SWAP_TOPIC]}))
            start = end + 1
        except Exception:
            if end - start < 10:
                raise
            LOG_CHUNK_SIZE_LOCAL = max(10, (end - start + 1) // 2)
            end = start + LOG_CHUNK_SIZE_LOCAL - 1
            logs.extend(web3.eth.get_logs({"address": PAIR_ADDRESS, "fromBlock": start, "toBlock": end, "topics": [SWAP_TOPIC]}))
            start = end + 1
    return logs


def receipt_flow_attribution(web3: Web3, tx_hash: str) -> tuple[Optional[str], Optional[str]]:
    """Return the direct IBS input source and direct USDT output recipient.

    These are evidence attached to the transaction, while tx.from remains the
    initiating account used for historical profiling. A router/aggregator may
    appear here, so the two addresses are deliberately preserved separately.
    """
    receipt = web3.eth.get_transaction_receipt(tx_hash)
    ibs_inputs: list[tuple[int, str]] = []
    usdt_outputs: list[tuple[int, str]] = []
    for log in receipt["logs"]:
        if len(log["topics"]) < 3 or Web3.to_hex(log["topics"][0]).lower() != TRANSFER_TOPIC.lower():
            continue
        contract = str(log["address"]).lower()
        sender = Web3.to_checksum_address("0x" + Web3.to_hex(log["topics"][1])[-40:])
        recipient = Web3.to_checksum_address("0x" + Web3.to_hex(log["topics"][2])[-40:])
        value = int.from_bytes(bytes(log["data"]), "big")
        if contract == IBS_ADDRESS.lower() and recipient.lower() == PAIR_ADDRESS.lower():
            ibs_inputs.append((value, sender))
        if contract == USDT_ADDRESS.lower() and sender.lower() == PAIR_ADDRESS.lower():
            usdt_outputs.append((value, recipient))
    token_source = max(ibs_inputs, default=(0, None), key=lambda x: x[0])[1]
    quote_recipient = max(usdt_outputs, default=(0, None), key=lambda x: x[0])[1]
    return token_source, quote_recipient


def qualifying_sellers(web3: Web3, logs: Iterable[Any], ibs_is_token0: bool, decimals: int) -> list[dict[str, Any]]:
    threshold_raw = int(SELL_THRESHOLD_IBS * (Decimal(10) ** decimals))
    tx_cache: dict[str, str] = {}
    flow_cache: dict[str, tuple[Optional[str], Optional[str]]] = {}
    found: list[dict[str, Any]] = []
    for log in logs:
        trade = decode_trade(log, ibs_is_token0)
        if trade is None or trade.side != "SELL" or trade.ibs_raw <= threshold_raw:
            continue
        event_id = f"{trade.tx_hash}:{trade.log_index}"
        if trade.tx_hash not in tx_cache:
            tx_cache[trade.tx_hash] = Web3.to_checksum_address(web3.eth.get_transaction(trade.tx_hash)["from"])
            flow_cache[trade.tx_hash] = receipt_flow_attribution(web3, trade.tx_hash)
        token_source, quote_recipient = flow_cache[trade.tx_hash]
        found.append({
            "event_id": event_id,
            "address": tx_cache[trade.tx_hash],
            "token_source": token_source,
            "quote_recipient": quote_recipient,
            "trade": trade,
        })
    return found


def alchemy_transfers(rpc_url: str, address: str, direction: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    page_key: Optional[str] = None
    while True:
        params: dict[str, Any] = {
            "fromBlock": "0x0", "toBlock": "latest", "contractAddresses": [IBS_ADDRESS],
            "category": ["erc20"], "excludeZeroValue": True, "withMetadata": True,
            "maxCount": "0x3e8", "order": "asc",
        }
        params["fromAddress" if direction == "out" else "toAddress"] = address
        if page_key:
            params["pageKey"] = page_key
        response = requests.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": "alchemy_getAssetTransfers", "params": [params]}, timeout=RPC_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(f"Alchemy Transfers API失败：{payload['error'].get('message', payload['error'])}")
        page = payload.get("result", {})
        result.extend(page.get("transfers", []))
        page_key = page.get("pageKey")
        if not page_key:
            return result


def transfer_time(item: dict[str, Any]) -> Optional[datetime]:
    value = item.get("metadata", {}).get("blockTimestamp")
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def transfer_raw(item: dict[str, Any], decimals: int) -> int:
    raw = item.get("rawContract", {}).get("value")
    if raw:
        return int(raw, 16)
    return int(Decimal(str(item.get("value", 0))) * (Decimal(10) ** decimals))


def historical_trades(web3: Web3, transfers: list[dict[str, Any]], ibs_is_token0: bool) -> list[Trade]:
    timestamps: dict[str, Optional[datetime]] = {}
    hashes: list[str] = []
    for item in transfers:
        tx_hash = str(item.get("hash", "")).lower()
        if tx_hash and tx_hash not in timestamps:
            hashes.append(tx_hash)
            timestamps[tx_hash] = transfer_time(item)
    trades: list[Trade] = []
    for tx_hash in hashes:
        receipt = web3.eth.get_transaction_receipt(tx_hash)
        for log in receipt["logs"]:
            if str(log["address"]).lower() != PAIR_ADDRESS.lower() or not log["topics"] or Web3.to_hex(log["topics"][0]).lower() != SWAP_TOPIC.lower():
                continue
            decoded = decode_trade(log, ibs_is_token0)
            if decoded:
                trades.append(Trade(decoded.tx_hash, decoded.block_number, decoded.log_index, decoded.side, decoded.ibs_raw, decoded.usdt_raw, timestamps[tx_hash]))
    unique = {(t.tx_hash, t.log_index): t for t in trades}
    return sorted(unique.values(), key=lambda t: (t.block_number, t.log_index))


def fifo_holding_hours(trades: list[Trade]) -> list[Decimal]:
    lots: list[list[Any]] = []
    durations: list[Decimal] = []
    for trade in trades:
        if trade.side == "BUY":
            lots.append([trade.ibs_raw, trade.timestamp])
            continue
        remaining = trade.ibs_raw
        while remaining > 0 and lots:
            used = min(remaining, lots[0][0])
            if trade.timestamp and lots[0][1]:
                durations.append(Decimal(str((trade.timestamp - lots[0][1]).total_seconds() / 3600)))
            lots[0][0] -= used
            remaining -= used
            if lots[0][0] == 0:
                lots.pop(0)
    return durations


def round_trip_metrics(trades: list[Trade]) -> dict[str, Any]:
    """Match pool buys and sells without assigning a cost to transferred IBS."""
    lots: list[list[Any]] = []
    durations: list[Decimal] = []
    matched_ibs_raw = 0
    matched_cost_raw = Decimal(0)
    matched_proceeds_raw = Decimal(0)
    profitable_legs = 0
    matched_legs = 0
    for trade in trades:
        if trade.side == "BUY":
            lots.append([trade.ibs_raw, Decimal(trade.usdt_raw) / Decimal(trade.ibs_raw), trade.timestamp])
            continue
        remaining = trade.ibs_raw
        sale_unit = Decimal(trade.usdt_raw) / Decimal(trade.ibs_raw)
        while remaining > 0 and lots:
            used = min(remaining, lots[0][0])
            cost = Decimal(used) * lots[0][1]
            proceeds = Decimal(used) * sale_unit
            matched_ibs_raw += used
            matched_cost_raw += cost
            matched_proceeds_raw += proceeds
            matched_legs += 1
            if proceeds > cost:
                profitable_legs += 1
            if trade.timestamp and lots[0][2]:
                durations.append(Decimal(str((trade.timestamp - lots[0][2]).total_seconds() / 3600)))
            lots[0][0] -= used
            remaining -= used
            if lots[0][0] == 0:
                lots.pop(0)
    short_ratio = (sum(d <= 24 for d in durations) / len(durations)) if durations else 0
    return {
        "matched_ibs_raw": matched_ibs_raw,
        "matched_cost_raw": matched_cost_raw,
        "matched_proceeds_raw": matched_proceeds_raw,
        "matched_pnl_raw": matched_proceeds_raw - matched_cost_raw,
        "matched_legs": matched_legs,
        "profitable_legs": profitable_legs,
        "durations": durations,
        "short_ratio": short_ratio,
    }


def add_dynamic_protocol_labels(web3: Web3) -> None:
    ibs = web3.eth.contract(IBS_ADDRESS, abi=ERC20_ABI)
    for label, function_name in (("IBS Treasury", "treasury"), ("IBS Tax Treasury", "taxTreasury")):
        try:
            address = Web3.to_checksum_address(getattr(ibs.functions, function_name)().call())
            PROTOCOL_LABELS[address.lower()] = label
        except Exception as exc:
            print(f"读取动态协议地址 {function_name} 失败，继续使用静态地址表：{exc}")


def classify(
    address: str,
    is_contract: bool,
    trades: list[Trade],
    protocol_source_raw: int,
    external_in_raw: int,
    first_buy: Optional[datetime],
    external_sender_count: int = 0,
) -> tuple[str, list[str], str]:
    key = address.lower()
    if key in PROTOCOL_LABELS:
        return f"平台/协议地址（{PROTOCOL_LABELS[key]}）", ["命中已知POTS协议地址表"], "不适用"
    if is_contract:
        return "合约/聚合器地址", ["地址存在合约代码，tx.from未必是最终受益人"], "需解析合约内部调用"
    buys = sum(t.side == "BUY" for t in trades)
    sells = sum(t.side == "SELL" for t in trades)
    buy_raw = sum(t.ibs_raw for t in trades if t.side == "BUY")
    sell_raw = sum(t.ibs_raw for t in trades if t.side == "SELL")
    excess_sell_raw = max(0, sell_raw - buy_raw)
    metrics = round_trip_metrics(trades)
    if external_sender_count >= 5 and sells >= 3 and excess_sell_raw > 0:
        return (
            "团队长/归集地址疑似",
            [f"从{external_sender_count}个外部地址接收IBS", "累计卖出量超过本池可见买入量", "存在归集后持续变现特征"],
            "中等置信度行为聚类；仍需推荐关系或链外身份佐证",
        )
    if protocol_source_raw > 0 and sells > 0:
        reasons = ["曾从质押/奖励/国库等协议地址收到IBS"]
        if excess_sell_raw > 0:
            reasons.append("累计卖出量超过本池可见买入量")
        return "协议来源IBS变现地址", reasons, "更像奖励或解押变现，未证实套利"
    if external_in_raw > 0 and excess_sell_raw > 0:
        return "外部转入IBS变现地址", ["卖出量超过本池买入量", "成本可能在其他地址、池或交易所"], "无法仅凭本池确认套利"
    if (
        buys >= 3 and sells >= 3 and metrics["matched_legs"] >= 3
        and metrics["short_ratio"] >= 0.6 and metrics["matched_pnl_raw"] > 0
    ):
        return (
            "短周期盈利交易地址（疑似套利）",
            [f"买{buys}次、卖{sells}次", f"{metrics['short_ratio']:.0%}已匹配仓位在24小时内卖出", "可确认成本部分总体盈利"],
            "发现可重复的短周期正利润闭环，仍需核对Gas和其他池",
        )
    if buys >= 3 and sells >= 3:
        return "高频交易地址（未证实套利）", [f"买{buys}次、卖{sells}次", "未形成可确认的短周期正利润闭环"], "未发现可验证套利方法"
    if sells > 0 and buys == 0:
        return "未知成本卖出地址", ["本池有卖出但没有可见买入"], "成本来源不足，不能判断套利"
    return "普通交易地址", ["未发现协议来源变现或可重复盈利闭环"], "未发现可验证套利方法"


def profile_wallet(web3: Web3, rpc_url: str, address: str, ibs_is_token0: bool, ibs_decimals: int, usdt_decimals: int, current_price: Decimal) -> dict[str, Any]:
    inbound = alchemy_transfers(rpc_url, address, "in")
    outbound = alchemy_transfers(rpc_url, address, "out")
    all_transfers = inbound + outbound
    trades = historical_trades(web3, all_transfers, ibs_is_token0)
    buys = [t for t in trades if t.side == "BUY"]
    sells = [t for t in trades if t.side == "SELL"]
    buy_ibs_raw = sum(t.ibs_raw for t in buys)
    sell_ibs_raw = sum(t.ibs_raw for t in sells)
    buy_usdt_raw = sum(t.usdt_raw for t in buys)
    sell_usdt_raw = sum(t.usdt_raw for t in sells)
    avg_cost = (Decimal(buy_usdt_raw) / Decimal(buy_ibs_raw)) * (Decimal(10) ** (ibs_decimals - usdt_decimals)) if buy_ibs_raw else None
    metrics = round_trip_metrics(trades)
    matched_ibs_raw = int(metrics["matched_ibs_raw"])
    realized_usdt = Decimal(metrics["matched_pnl_raw"]) / (Decimal(10) ** usdt_decimals)
    wallet_balance_raw = int(web3.eth.contract(IBS_ADDRESS, abi=ERC20_ABI).functions.balanceOf(address).call())
    staked_in_raw = sum(transfer_raw(x, ibs_decimals) for x in outbound if str(x.get("to", "")).lower() == STAKING_ADDRESS.lower())
    staked_out_raw = sum(transfer_raw(x, ibs_decimals) for x in inbound if str(x.get("from", "")).lower() == STAKING_ADDRESS.lower())
    staked_est_raw = max(0, staked_in_raw - staked_out_raw)
    staking_return_raw = sum(transfer_raw(x, ibs_decimals) for x in inbound if str(x.get("from", "")).lower() == STAKING_ADDRESS.lower())
    protocol_direct_raw = sum(transfer_raw(x, ibs_decimals) for x in inbound if str(x.get("from", "")).lower() in PROTOCOL_LABELS and str(x.get("from", "")).lower() not in {PAIR_ADDRESS.lower(), STAKING_ADDRESS.lower()})
    protocol_source_raw = protocol_direct_raw + staking_return_raw
    external_in_raw = sum(transfer_raw(x, ibs_decimals) for x in inbound if str(x.get("from", "")).lower() not in PROTOCOL_LABELS)
    external_senders = {
        str(x.get("from", "")).lower()
        for x in inbound
        if str(x.get("from", "")).lower() not in PROTOCOL_LABELS
        and str(x.get("from", "")).lower() not in {"", ZERO_ADDRESS}
    }
    external_out_raw = sum(transfer_raw(x, ibs_decimals) for x in outbound if str(x.get("to", "")).lower() not in PROTOCOL_LABELS)
    first_stake = min((transfer_time(x) for x in outbound if str(x.get("to", "")).lower() == STAKING_ADDRESS.lower() and transfer_time(x)), default=None)
    first_buy = buys[0].timestamp if buys else None
    first_activity = min((transfer_time(x) for x in all_transfers if transfer_time(x)), default=None)
    holding_hours = metrics["durations"]
    avg_holding_hours = (sum(holding_hours, Decimal(0)) / len(holding_hours)) if holding_hours else None
    is_contract = len(web3.eth.get_code(address)) > 0
    category, reasons, arbitrage_assessment = classify(
        address, is_contract, trades, protocol_source_raw, external_in_raw,
        first_buy, len(external_senders),
    )
    unpriced_sold_raw = max(0, sell_ibs_raw - matched_ibs_raw)
    unpriced_sale_proceeds_raw = (
        Decimal(sell_usdt_raw) * Decimal(unpriced_sold_raw) / Decimal(sell_ibs_raw)
        if sell_ibs_raw else Decimal(0)
    )
    protocol_sold_est_raw = min(unpriced_sold_raw, protocol_source_raw)
    external_sold_est_raw = min(max(0, unpriced_sold_raw - protocol_sold_est_raw), external_in_raw)
    protocol_sale_proceeds_est = (
        Decimal(sell_usdt_raw) * Decimal(protocol_sold_est_raw) / Decimal(sell_ibs_raw) / (Decimal(10) ** usdt_decimals)
        if sell_ibs_raw else Decimal(0)
    )
    cost_coverage = (Decimal(matched_ibs_raw) / Decimal(sell_ibs_raw) * 100) if sell_ibs_raw else Decimal(100)
    confidence = "较高" if unpriced_sold_raw == 0 else f"仅{cost_coverage:.1f}%卖出量可匹配本池买入成本"
    return {
        "address": address, "category": category, "reasons": reasons,
        "first_activity": first_activity, "first_buy": first_buy, "first_stake": first_stake,
        "buy_count": len(buys), "sell_count": len(sells),
        "buy_ibs_raw": buy_ibs_raw, "sell_ibs_raw": sell_ibs_raw,
        "buy_usdt_raw": buy_usdt_raw, "sell_usdt_raw": sell_usdt_raw,
        "avg_cost": avg_cost, "realized_usdt": realized_usdt,
        "pnl_confidence": confidence, "wallet_balance_raw": wallet_balance_raw,
        "staked_est_raw": staked_est_raw, "protocol_in_raw": protocol_direct_raw,
        "staking_return_raw": staking_return_raw, "protocol_source_raw": protocol_source_raw,
        "external_in_raw": external_in_raw, "external_out_raw": external_out_raw,
        "external_sender_count": len(external_senders),
        "is_contract": is_contract, "avg_holding_hours": avg_holding_hours,
        "matched_ibs_raw": matched_ibs_raw, "unpriced_sold_raw": unpriced_sold_raw,
        "unpriced_sale_proceeds": unpriced_sale_proceeds_raw / (Decimal(10) ** usdt_decimals),
        "protocol_sold_est_raw": protocol_sold_est_raw,
        "external_sold_est_raw": external_sold_est_raw,
        "protocol_sale_proceeds_est": protocol_sale_proceeds_est,
        "cost_coverage": cost_coverage, "arbitrage_assessment": arbitrage_assessment,
        "short_ratio": Decimal(str(metrics["short_ratio"] * 100)),
    }


def fmt_time(value: Optional[datetime]) -> str:
    return "未发现" if value is None else value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def amount(raw: int, decimals: int) -> Decimal:
    return Decimal(raw) / (Decimal(10) ** decimals)


def anomaly_reasons(profile: dict[str, Any], ibs_decimals: int, usdt_decimals: int) -> list[str]:
    """Return evidence-based alert reasons; an ordinary >20 IBS sale returns []."""
    reasons: list[str] = []
    category = str(profile["category"])
    total_trades = int(profile["buy_count"]) + int(profile["sell_count"])
    net_outflow = amount(int(profile["sell_usdt_raw"]) - int(profile["buy_usdt_raw"]), usdt_decimals)
    if category.startswith("平台/协议地址"):
        reasons.append("已知平台/协议地址直接参与卖出")
    if "短周期盈利交易地址" in category:
        reasons.append("存在可重复的短周期正利润闭环")
    if "团队长/归集地址疑似" in category:
        reasons.append("多地址筹码归集后持续卖入LP")
    if (
        int(profile["protocol_sold_est_raw"]) > 0
        and Decimal(profile["protocol_sale_proceeds_est"]) >= ANOMALY_MIN_PROTOCOL_PROCEEDS_USDT
        and int(profile["sell_count"]) >= 3
    ):
        reasons.append("协议来源IBS持续变现并从LP取走较多USDT")
    if (
        total_trades >= ANOMALY_MIN_TRADES
        and int(profile["buy_count"]) >= 2
        and int(profile["sell_count"]) >= 2
        and net_outflow >= ANOMALY_MIN_NET_OUTFLOW_USDT
    ):
        reasons.append("高频交易且对LP形成显著净USDT流出")
    if (
        int(profile["external_sold_est_raw"]) > 0
        and int(profile["sell_count"]) >= ANOMALY_MIN_TRADES
        and Decimal(profile["unpriced_sale_proceeds"]) >= ANOMALY_MIN_NET_OUTFLOW_USDT
    ):
        reasons.append("外部转入筹码被高频卖入LP，成本来源不可见")
    return list(dict.fromkeys(reasons))


def anomaly_alert_due(profile: dict[str, Any], stored: Optional[dict[str, Any]], reasons: list[str], now: datetime) -> bool:
    if not reasons:
        return False
    if not stored or not stored.get("last_anomaly_alert_utc"):
        return True
    old_reasons = set(stored.get("anomaly_reasons", []))
    if not set(reasons).issubset(old_reasons):
        return True
    if int(profile["sell_count"]) - int(stored.get("last_alert_sell_count", 0)) >= ANOMALY_NEW_SELLS:
        return True
    last_alert = datetime.fromisoformat(str(stored["last_anomaly_alert_utc"]).replace("Z", "+00:00"))
    return (now - last_alert).total_seconds() >= ANOMALY_REPEAT_HOURS * 3600


def build_message(profile: dict[str, Any], trigger: Trade, ibs_decimals: int, usdt_decimals: int, lp_ibs_raw: int, lp_usdt_raw: int, alert_reasons: Optional[list[str]] = None) -> str:
    address = profile["address"]
    pnl_icon = "🟢" if profile["realized_usdt"] >= 0 else "🔴"
    reason_text = "；".join(html.escape(x) for x in profile["reasons"])
    ibs_pool_share = Decimal(trigger.ibs_raw) / Decimal(lp_ibs_raw) * 100 if lp_ibs_raw else Decimal(0)
    usdt_pool_share = Decimal(trigger.usdt_raw) / Decimal(lp_usdt_raw) * 100 if lp_usdt_raw else Decimal(0)
    holding_text = "暂无可匹配买卖" if profile["avg_holding_hours"] is None else f"{profile['avg_holding_hours']:,.1f} 小时"
    alert_text = "；".join(html.escape(x) for x in (alert_reasons or profile["reasons"]))
    attribution_lines: list[str] = []
    if trigger.token_source and trigger.token_source.lower() != address.lower():
        attribution_lines.append(f"IBS直接入池地址：<code>{trigger.token_source}</code>")
    if trigger.quote_recipient and trigger.quote_recipient.lower() != address.lower():
        attribution_lines.append(f"USDT直接接收地址：<code>{trigger.quote_recipient}</code>")
    return "\n".join([
        "🚨 <b>IBS异常账户提醒</b>",
        f"异常原因：<b>{alert_text}</b>",
        f"触发卖单：<b>{amount(trigger.ibs_raw, ibs_decimals):,.4f} IBS</b> ｜ <b>{amount(trigger.usdt_raw, usdt_decimals):,.2f} USDT</b>",
        f"本单占LP：IBS储备 {ibs_pool_share:.4f}% ｜ USDT储备 {usdt_pool_share:.4f}%",
        f"地址：<code>{address}</code>",
        *attribution_lines,
        f"行为判断：<b>{html.escape(profile['category'])}</b>",
        f"依据：{reason_text}", "",
        f"套利路径：<b>{html.escape(profile['arbitrage_assessment'])}</b>",
        f"LP累计流出：<b>{amount(profile['sell_usdt_raw'], usdt_decimals):,.2f} USDT</b>（该地址全部可见卖出）",
        f"本池买入成本覆盖：{profile['cost_coverage']:.1f}% 的卖出IBS", "",
        f"首次IBS活动：{fmt_time(profile['first_activity'])}",
        f"首次买入：{fmt_time(profile['first_buy'])}",
        f"首次质押：{fmt_time(profile['first_stake'])}",
        f"平均已匹配持仓时间：{holding_text}",
        f"历史买卖：买 <b>{profile['buy_count']}</b> 次 / 卖 <b>{profile['sell_count']}</b> 次",
        f"累计买入：{amount(profile['buy_ibs_raw'], ibs_decimals):,.4f} IBS ｜ 花费 {amount(profile['buy_usdt_raw'], usdt_decimals):,.2f} USDT",
        f"累计卖出：{amount(profile['sell_ibs_raw'], ibs_decimals):,.4f} IBS ｜ 收回 {amount(profile['sell_usdt_raw'], usdt_decimals):,.2f} USDT",
        f"当前钱包：{amount(profile['wallet_balance_raw'], ibs_decimals):,.4f} IBS ｜ 质押净转入估算 {amount(profile['staked_est_raw'], ibs_decimals):,.4f} IBS",
        f"{pnl_icon} 可确认成本部分盈亏：<b>{profile['realized_usdt']:+,.2f} USDT</b>",
        f"未知成本卖出：{amount(profile['unpriced_sold_raw'], ibs_decimals):,.4f} IBS ｜ 对应收入 {profile['unpriced_sale_proceeds']:,.2f} USDT",
        f"协议来源卖出上限估算：{amount(profile['protocol_sold_est_raw'], ibs_decimals):,.4f} IBS ｜ 对应LP流出约 {profile['protocol_sale_proceeds_est']:,.2f} USDT",
        f"盈亏口径：{html.escape(profile['pnl_confidence'])}；未知成本部分不计为利润",
        f"外部转入/转出：{amount(profile['external_in_raw'], ibs_decimals):,.4f} / {amount(profile['external_out_raw'], ibs_decimals):,.4f} IBS",
        f"外部IBS来源地址数：{profile.get('external_sender_count', 0)}",
        f"质押合约返还/奖励：{amount(profile['staking_return_raw'], ibs_decimals):,.4f} IBS（本金与奖励无法拆分）",
        f"其他协议地址转入：{amount(profile['protocol_in_raw'], ibs_decimals):,.4f} IBS", "",
        f'<a href="https://bscscan.com/address/{address}">查看地址</a> ｜ <a href="https://bscscan.com/tx/{trigger.tx_hash}">查看触发交易</a>',
        "说明：只在异常条件首次出现、升级、卖出次数显著增加或定期复查时通知。只有本池买入→卖出的可匹配部分计算盈亏；“协议来源异常变现”不等于老鼠仓，是否关联平台仍需链外证据。",
    ])


def send_telegram(token: str, chat_id: str, message: str) -> None:
    response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=TELEGRAM_TIMEOUT_SECONDS)
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError(f"Telegram发送失败：{response.text}")


def current_pair_meta(web3: Web3) -> tuple[bool, int, int, Decimal, int, int]:
    pair = web3.eth.contract(PAIR_ADDRESS, abi=PAIR_ABI)
    token0 = Web3.to_checksum_address(pair.functions.token0().call())
    token1 = Web3.to_checksum_address(pair.functions.token1().call())
    if {token0.lower(), token1.lower()} != {IBS_ADDRESS.lower(), USDT_ADDRESS.lower()}:
        raise RuntimeError("LP代币配置不匹配")
    ibs_decimals = int(web3.eth.contract(IBS_ADDRESS, abi=ERC20_ABI).functions.decimals().call())
    usdt_decimals = int(web3.eth.contract(USDT_ADDRESS, abi=ERC20_ABI).functions.decimals().call())
    reserve0, reserve1, _ = pair.functions.getReserves().call()
    ibs_raw, usdt_raw = (reserve0, reserve1) if token0.lower() == IBS_ADDRESS.lower() else (reserve1, reserve0)
    price = amount(usdt_raw, usdt_decimals) / amount(ibs_raw, ibs_decimals)
    return token0.lower() == IBS_ADDRESS.lower(), ibs_decimals, usdt_decimals, price, int(ibs_raw), int(usdt_raw)


def main() -> None:
    rpc_url, bot_token, chat_id = require_env("BSC_RPC"), require_env("BOT_TOKEN"), require_env("CHAT_ID")
    web3 = connect_web3(rpc_url)
    ibs_is_token0, ibs_decimals, usdt_decimals, current_price, lp_ibs_raw, lp_usdt_raw = current_pair_meta(web3)
    add_dynamic_protocol_labels(web3)
    state = load_state()
    current_block = int(web3.eth.block_number) - CONFIRMATION_BLOCKS
    if state.get("last_scanned_block") is None:
        state["last_scanned_block"] = current_block
        save_state(state)
        print(f"地址画像已建立基线区块 {current_block}；首次启用不补发历史卖单")
        return
    start = int(state["last_scanned_block"]) + 1
    end = min(current_block, start + MAX_SCAN_BLOCKS - 1)
    if start > end:
        print("没有新的确认区块")
        return
    sellers = qualifying_sellers(web3, get_swap_logs(web3, start, end), ibs_is_token0, ibs_decimals)
    seen = set(state.get("seen", []))
    pending = compact_pending_by_address(state.get("pending", []))
    for item in sellers:
        if item["event_id"] in seen:
            continue
        trade: Trade = item["trade"]
        existing = next((x for x in pending if x.get("address", "").lower() == item["address"].lower()), None)
        if existing is None:
            pending.append({"event_id": item["event_id"], "event_ids": [item["event_id"]], "address": item["address"], "token_source": item.get("token_source"), "quote_recipient": item.get("quote_recipient"), "tx_hash": trade.tx_hash, "block_number": trade.block_number, "log_index": trade.log_index, "ibs_raw": str(trade.ibs_raw), "usdt_raw": str(trade.usdt_raw)})
        else:
            existing.setdefault("event_ids", [existing["event_id"]]).append(item["event_id"])
            if (trade.block_number, trade.log_index) > (int(existing["block_number"]), int(existing["log_index"])):
                existing.update({"event_id": item["event_id"], "token_source": item.get("token_source"), "quote_recipient": item.get("quote_recipient"), "tx_hash": trade.tx_hash, "block_number": trade.block_number, "log_index": trade.log_index, "ibs_raw": str(trade.ibs_raw), "usdt_raw": str(trade.usdt_raw)})
    state["last_scanned_block"] = end
    state["pending"] = pending
    save_state(state)
    processed = 0
    alerts = 0
    while state["pending"] and processed < MAX_PROFILES_PER_RUN:
        record = state["pending"][0]
        trigger = Trade(
            record["tx_hash"], int(record["block_number"]), int(record["log_index"]),
            "SELL", int(record["ibs_raw"]), int(record["usdt_raw"]),
            token_source=record.get("token_source"), quote_recipient=record.get("quote_recipient"),
        )
        profile = profile_wallet(web3, rpc_url, Web3.to_checksum_address(record["address"]), ibs_is_token0, ibs_decimals, usdt_decimals, current_price)
        key = record["address"].lower()
        stored = state["profiles"].get(key)
        reasons = anomaly_reasons(profile, ibs_decimals, usdt_decimals)
        if amount(trigger.usdt_raw, usdt_decimals) >= LARGE_SELL_USDT:
            reasons.append(f"单笔从LP取走至少{LARGE_SELL_USDT:,.0f} USDT")
        checked_at = datetime.now(timezone.utc)
        should_alert = anomaly_alert_due(profile, stored, reasons, checked_at)
        if should_alert:
            send_telegram(bot_token, chat_id, build_message(profile, trigger, ibs_decimals, usdt_decimals, lp_ibs_raw, lp_usdt_raw, reasons))
            alerts += 1
        saved_profile = {k: (v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, Decimal) else v) for k, v in profile.items()}
        saved_profile["anomaly_reasons"] = reasons
        if should_alert:
            saved_profile["last_anomaly_alert_utc"] = checked_at.isoformat()
            saved_profile["last_alert_sell_count"] = profile["sell_count"]
        elif stored:
            for field in ("last_anomaly_alert_utc", "last_alert_sell_count"):
                if field in stored:
                    saved_profile[field] = stored[field]
        state["profiles"][key] = saved_profile
        state["seen"] = (state.get("seen", []) + record.get("event_ids", [record["event_id"]]))[-2000:]
        state["pending"].pop(0)
        save_state(state)
        processed += 1
    print(f"扫描区块 {start}-{end}，发现{len(sellers)}笔>20 IBS卖出，复核{processed}个地址，异常通知{alerts}个")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"IBS地址画像失败：{exc}", flush=True)
        raise
