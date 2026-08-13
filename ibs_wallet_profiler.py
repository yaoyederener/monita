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
STATE_FILE = Path(os.getenv("WALLET_PROFILE_STATE_FILE", "data/ibs_wallet_profiles.json"))

CONFIRMATION_BLOCKS = int(os.getenv("CONFIRMATION_BLOCKS", "3"))
SELL_THRESHOLD_IBS = Decimal(os.getenv("SELL_THRESHOLD_IBS", "20"))
MAX_SCAN_BLOCKS = int(os.getenv("MAX_SCAN_BLOCKS", "20000"))
LOG_CHUNK_SIZE = int(os.getenv("LOG_CHUNK_SIZE", "2000"))
MAX_PROFILES_PER_RUN = int(os.getenv("MAX_PROFILES_PER_RUN", "2"))
RPC_TIMEOUT_SECONDS = int(os.getenv("RPC_TIMEOUT_SECONDS", "30"))
TELEGRAM_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "20"))

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


def qualifying_sellers(web3: Web3, logs: Iterable[Any], ibs_is_token0: bool, decimals: int) -> list[dict[str, Any]]:
    threshold_raw = int(SELL_THRESHOLD_IBS * (Decimal(10) ** decimals))
    tx_cache: dict[str, str] = {}
    found: list[dict[str, Any]] = []
    for log in logs:
        trade = decode_trade(log, ibs_is_token0)
        if trade is None or trade.side != "SELL" or trade.ibs_raw <= threshold_raw:
            continue
        event_id = f"{trade.tx_hash}:{trade.log_index}"
        if trade.tx_hash not in tx_cache:
            tx_cache[trade.tx_hash] = Web3.to_checksum_address(web3.eth.get_transaction(trade.tx_hash)["from"])
        found.append({"event_id": event_id, "address": tx_cache[trade.tx_hash], "trade": trade})
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


def add_dynamic_protocol_labels(web3: Web3) -> None:
    ibs = web3.eth.contract(IBS_ADDRESS, abi=ERC20_ABI)
    for label, function_name in (("IBS Treasury", "treasury"), ("IBS Tax Treasury", "taxTreasury")):
        try:
            address = Web3.to_checksum_address(getattr(ibs.functions, function_name)().call())
            PROTOCOL_LABELS[address.lower()] = label
        except Exception as exc:
            print(f"读取动态协议地址 {function_name} 失败，继续使用静态地址表：{exc}")


def classify(address: str, is_contract: bool, trades: list[Trade], protocol_in_raw: int, first_buy: Optional[datetime]) -> tuple[str, list[str]]:
    key = address.lower()
    if key in PROTOCOL_LABELS:
        return f"平台/协议地址（{PROTOCOL_LABELS[key]}）", ["命中已知POTS协议地址表"]
    if is_contract:
        return "合约/聚合器地址", ["地址存在合约代码，tx.from未必是最终受益人"]
    buys = sum(t.side == "BUY" for t in trades)
    sells = sum(t.side == "SELL" for t in trades)
    durations = fifo_holding_hours(trades)
    short_ratio = (sum(d <= 24 for d in durations) / len(durations)) if durations else 0
    if buys >= 3 and sells >= 3 and short_ratio >= 0.6:
        return "高频套利地址", [f"买{buys}次、卖{sells}次", f"{short_ratio:.0%}已匹配仓位在24小时内卖出"]
    suspicious: list[str] = []
    if protocol_in_raw > 0:
        suspicious.append("曾直接从协议地址收到IBS")
    if sells > 0 and buys == 0:
        suspicious.append("有卖出但本池没有可见买入成本")
    if first_buy and first_buy < datetime(2026, 5, 1, tzinfo=timezone.utc):
        suspicious.append("项目早期建仓")
    if len(suspicious) >= 2:
        return "关联/老鼠仓高疑似", suspicious
    if suspicious:
        return "关联风险待观察", suspicious
    return "普通交易地址", ["未命中协议、合约、高频套利或明显关联信号"]


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
    known_sold_raw = min(sell_ibs_raw, buy_ibs_raw)
    # When the wallet sold more IBS than it visibly bought in this pool, only
    # match the same proportion of sale proceeds to known-cost inventory.  The
    # remaining proceeds came from transfers/rewards/other venues and must not
    # be presented as known realized profit.
    matched_sale_proceeds_raw = (
        Decimal(sell_usdt_raw) * Decimal(known_sold_raw) / Decimal(sell_ibs_raw)
        if sell_ibs_raw
        else Decimal(0)
    )
    matched_buy_cost_raw = (
        Decimal(known_sold_raw) * Decimal(buy_usdt_raw) / Decimal(buy_ibs_raw)
        if buy_ibs_raw
        else Decimal(0)
    )
    realized_raw = matched_sale_proceeds_raw - matched_buy_cost_raw
    realized_usdt = realized_raw / (Decimal(10) ** usdt_decimals)
    wallet_balance_raw = int(web3.eth.contract(IBS_ADDRESS, abi=ERC20_ABI).functions.balanceOf(address).call())
    staked_in_raw = sum(transfer_raw(x, ibs_decimals) for x in outbound if str(x.get("to", "")).lower() == STAKING_ADDRESS.lower())
    staked_out_raw = sum(transfer_raw(x, ibs_decimals) for x in inbound if str(x.get("from", "")).lower() == STAKING_ADDRESS.lower())
    staked_est_raw = max(0, staked_in_raw - staked_out_raw)
    protocol_in_raw = sum(transfer_raw(x, ibs_decimals) for x in inbound if str(x.get("from", "")).lower() in PROTOCOL_LABELS and str(x.get("from", "")).lower() not in {PAIR_ADDRESS.lower(), STAKING_ADDRESS.lower()})
    external_in_raw = sum(transfer_raw(x, ibs_decimals) for x in inbound if str(x.get("from", "")).lower() not in PROTOCOL_LABELS)
    external_out_raw = sum(transfer_raw(x, ibs_decimals) for x in outbound if str(x.get("to", "")).lower() not in PROTOCOL_LABELS)
    first_stake = min((transfer_time(x) for x in outbound if str(x.get("to", "")).lower() == STAKING_ADDRESS.lower() and transfer_time(x)), default=None)
    first_buy = buys[0].timestamp if buys else None
    first_activity = min((transfer_time(x) for x in all_transfers if transfer_time(x)), default=None)
    holding_hours = fifo_holding_hours(trades)
    avg_holding_hours = (sum(holding_hours, Decimal(0)) / len(holding_hours)) if holding_hours else None
    is_contract = len(web3.eth.get_code(address)) > 0
    category, reasons = classify(address, is_contract, trades, protocol_in_raw, first_buy)
    mark_to_market = (Decimal(wallet_balance_raw + staked_est_raw) / (Decimal(10) ** ibs_decimals)) * current_price
    total_pnl = (Decimal(sell_usdt_raw - buy_usdt_raw) / (Decimal(10) ** usdt_decimals)) + mark_to_market
    complete_cost = sell_ibs_raw <= buy_ibs_raw and protocol_in_raw == 0 and external_in_raw == 0
    confidence = "较高" if complete_cost else "有限（存在转账/奖励或卖出量超过可见买入）"
    return {
        "address": address, "category": category, "reasons": reasons,
        "first_activity": first_activity, "first_buy": first_buy, "first_stake": first_stake,
        "buy_count": len(buys), "sell_count": len(sells),
        "buy_ibs_raw": buy_ibs_raw, "sell_ibs_raw": sell_ibs_raw,
        "buy_usdt_raw": buy_usdt_raw, "sell_usdt_raw": sell_usdt_raw,
        "avg_cost": avg_cost, "realized_usdt": realized_usdt, "total_pnl": total_pnl,
        "pnl_confidence": confidence, "wallet_balance_raw": wallet_balance_raw,
        "staked_est_raw": staked_est_raw, "protocol_in_raw": protocol_in_raw,
        "external_in_raw": external_in_raw, "external_out_raw": external_out_raw,
        "is_contract": is_contract, "avg_holding_hours": avg_holding_hours,
    }


def fmt_time(value: Optional[datetime]) -> str:
    return "未发现" if value is None else value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def amount(raw: int, decimals: int) -> Decimal:
    return Decimal(raw) / (Decimal(10) ** decimals)


def build_message(profile: dict[str, Any], trigger: Trade, ibs_decimals: int, usdt_decimals: int, lp_ibs_raw: int, lp_usdt_raw: int) -> str:
    address = profile["address"]
    pnl_icon = "🟢" if profile["total_pnl"] >= 0 else "🔴"
    reason_text = "；".join(html.escape(x) for x in profile["reasons"])
    ibs_pool_share = Decimal(trigger.ibs_raw) / Decimal(lp_ibs_raw) * 100 if lp_ibs_raw else Decimal(0)
    usdt_pool_share = Decimal(trigger.usdt_raw) / Decimal(lp_usdt_raw) * 100 if lp_usdt_raw else Decimal(0)
    holding_text = "暂无可匹配买卖" if profile["avg_holding_hours"] is None else f"{profile['avg_holding_hours']:,.1f} 小时"
    return "\n".join([
        "🔎 <b>IBS卖家地址画像</b>",
        f"触发卖单：<b>{amount(trigger.ibs_raw, ibs_decimals):,.4f} IBS</b> ｜ <b>{amount(trigger.usdt_raw, usdt_decimals):,.2f} USDT</b>",
        f"本单占LP：IBS储备 {ibs_pool_share:.4f}% ｜ USDT储备 {usdt_pool_share:.4f}%",
        f"地址：<code>{address}</code>",
        f"类型判断：<b>{html.escape(profile['category'])}</b>",
        f"依据：{reason_text}", "",
        f"首次IBS活动：{fmt_time(profile['first_activity'])}",
        f"首次买入：{fmt_time(profile['first_buy'])}",
        f"首次质押：{fmt_time(profile['first_stake'])}",
        f"平均已匹配持仓时间：{holding_text}",
        f"历史买卖：买 <b>{profile['buy_count']}</b> 次 / 卖 <b>{profile['sell_count']}</b> 次",
        f"累计买入：{amount(profile['buy_ibs_raw'], ibs_decimals):,.4f} IBS ｜ 花费 {amount(profile['buy_usdt_raw'], usdt_decimals):,.2f} USDT",
        f"累计卖出：{amount(profile['sell_ibs_raw'], ibs_decimals):,.4f} IBS ｜ 收回 {amount(profile['sell_usdt_raw'], usdt_decimals):,.2f} USDT",
        f"当前钱包：{amount(profile['wallet_balance_raw'], ibs_decimals):,.4f} IBS ｜ 质押净转入估算 {amount(profile['staked_est_raw'], ibs_decimals):,.4f} IBS",
        f"已实现盈亏估算：{profile['realized_usdt']:+,.2f} USDT",
        f"{pnl_icon} 总盈亏估算：<b>{profile['total_pnl']:+,.2f} USDT</b>",
        f"盈亏可信度：{html.escape(profile['pnl_confidence'])}",
        f"外部转入/转出：{amount(profile['external_in_raw'], ibs_decimals):,.4f} / {amount(profile['external_out_raw'], ibs_decimals):,.4f} IBS",
        f"协议地址转入：{amount(profile['protocol_in_raw'], ibs_decimals):,.4f} IBS", "",
        f'<a href="https://bscscan.com/address/{address}">查看地址</a> ｜ <a href="https://bscscan.com/tx/{trigger.tx_hash}">查看触发交易</a>',
        "说明：分类是链上行为风险标签，不是对个人身份或违法行为的定性；盈亏不含其他交易池、CEX、手续费和无法识别的场外成本。",
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
    pending = list(state.get("pending", []))
    for item in sellers:
        if item["event_id"] not in seen and all(x.get("event_id") != item["event_id"] for x in pending):
            trade: Trade = item["trade"]
            pending.append({"event_id": item["event_id"], "address": item["address"], "tx_hash": trade.tx_hash, "block_number": trade.block_number, "log_index": trade.log_index, "ibs_raw": str(trade.ibs_raw), "usdt_raw": str(trade.usdt_raw)})
    state["last_scanned_block"] = end
    state["pending"] = pending
    save_state(state)
    processed = 0
    while state["pending"] and processed < MAX_PROFILES_PER_RUN:
        record = state["pending"][0]
        trigger = Trade(record["tx_hash"], int(record["block_number"]), int(record["log_index"]), "SELL", int(record["ibs_raw"]), int(record["usdt_raw"]))
        profile = profile_wallet(web3, rpc_url, Web3.to_checksum_address(record["address"]), ibs_is_token0, ibs_decimals, usdt_decimals, current_price)
        send_telegram(bot_token, chat_id, build_message(profile, trigger, ibs_decimals, usdt_decimals, lp_ibs_raw, lp_usdt_raw))
        state["profiles"][record["address"].lower()] = {k: (v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, Decimal) else v) for k, v in profile.items()}
        state["seen"] = (state.get("seen", []) + [record["event_id"]])[-2000:]
        state["pending"].pop(0)
        save_state(state)
        processed += 1
    print(f"扫描区块 {start}-{end}，发现{len(sellers)}笔>20 IBS卖出，完成{processed}个地址画像")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"IBS地址画像失败：{exc}", flush=True)
        raise
