import json
import os
import sys
import time
from decimal import Decimal
from typing import Any

import requests
from web3 import Web3


# ============================================================
# 监控配置
# ============================================================

PAIR_ADDRESS = Web3.to_checksum_address(
    "0x2a4B99A9c4544D35e8D266111c50B67fEA01d53d"
)

IBS_ADDRESS = Web3.to_checksum_address(
    "0x255e746aBb8D9Acac00d6d023e5E63E3b8DFA7cd"
)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DEAD_ADDRESS = "0x000000000000000000000000000000000000dead"

# 识别为零地址/黑洞地址的来源。
# 注意：ZERO -> 用户是标准ERC-20增发日志形式；
# DEAD -> 用户并非标准增发，只标记为“黑洞地址来源”。
BURN_ADDRESSES = {
    ZERO_ADDRESS.lower(),
    DEAD_ADDRESS.lower(),
}

# 达到50 IBS才发送Telegram通知
MIN_IBS_AMOUNT = Decimal("50")

# 区块进度文件
LAST_BLOCK_FILE = "last_block.txt"

# 保存“曾直接收到零地址/黑洞地址转入IBS”的地址
MINT_ORIGIN_FILE = "mint_origin_addresses.json"

# 等待20个确认区块
CONFIRMATION_BLOCKS = 20

# Alchemy BSC单次查询10个区块
BLOCK_CHUNK_SIZE = 10

# 每次运行最多扫描1000个区块
MAX_BLOCKS_PER_RUN = 1000

# 最长运行330秒
MAX_RUNTIME_SECONDS = 330

# RPC和Telegram重试
MAX_RETRIES = 5
RETRY_BASE_SECONDS = 3

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
BSC_RPC = os.environ.get("BSC_RPC", "").strip()

TRANSFER_TOPIC = Web3.keccak(
    text="Transfer(address,address,uint256)"
).hex().lower()


# ============================================================
# 最小ABI
# ============================================================

PAIR_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {
                "indexed": True,
                "internalType": "address",
                "name": "sender",
                "type": "address",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "amount0In",
                "type": "uint256",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "amount1In",
                "type": "uint256",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "amount0Out",
                "type": "uint256",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "amount1Out",
                "type": "uint256",
            },
            {
                "indexed": True,
                "internalType": "address",
                "name": "to",
                "type": "address",
            },
        ],
        "name": "Swap",
        "type": "event",
    },
    {
        "inputs": [],
        "name": "token0",
        "outputs": [
            {
                "internalType": "address",
                "name": "",
                "type": "address",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token1",
        "outputs": [
            {
                "internalType": "address",
                "name": "",
                "type": "address",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

TOKEN_ABI = [
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [
            {
                "internalType": "uint8",
                "name": "",
                "type": "uint8",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [
            {
                "internalType": "string",
                "name": "",
                "type": "string",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


# ============================================================
# RPC连接
# ============================================================

def connect_web3() -> Web3:
    if not BSC_RPC:
        raise RuntimeError("没有设置GitHub Secret：BSC_RPC")

    print("正在连接Alchemy BSC RPC……", flush=True)

    web3 = Web3(
        Web3.HTTPProvider(
            BSC_RPC,
            request_kwargs={"timeout": 30},
        )
    )

    if not web3.is_connected():
        raise RuntimeError("Alchemy RPC连接失败，请检查BSC_RPC")

    chain_id = int(web3.eth.chain_id)
    if chain_id != 56:
        raise RuntimeError(
            f"网络错误，当前Chain ID为{chain_id}，BSC Mainnet应为56"
        )

    latest_block = int(web3.eth.block_number)
    print("Alchemy RPC连接成功", flush=True)
    print(f"BSC最新区块：{latest_block}", flush=True)

    return web3


# ============================================================
# 区块进度
# ============================================================

def read_last_block(safe_latest: int) -> int:
    if not os.path.exists(LAST_BLOCK_FILE):
        print("没有last_block.txt，从当前区块开始", flush=True)
        return safe_latest - 1

    try:
        with open(LAST_BLOCK_FILE, "r", encoding="utf-8") as file:
            content = file.read().strip()

        if not content:
            print("last_block.txt为空，从当前区块开始", flush=True)
            return safe_latest - 1

        last_block = int(content)

        if last_block <= 0:
            print("首次运行，不补发历史交易", flush=True)
            return safe_latest - 1

        if last_block > safe_latest:
            print(
                "last_block高于当前安全区块，从当前安全区块继续",
                flush=True,
            )
            return safe_latest - 1

        print(f"上次扫描到区块：{last_block}", flush=True)
        return last_block

    except Exception as error:
        print(
            f"读取last_block.txt失败：{error}，从当前区块开始",
            flush=True,
        )
        return safe_latest - 1


def save_last_block(block_number: int) -> None:
    temporary_file = f"{LAST_BLOCK_FILE}.tmp"

    with open(temporary_file, "w", encoding="utf-8") as file:
        file.write(str(block_number))

    os.replace(temporary_file, LAST_BLOCK_FILE)
    print(f"已保存区块：{block_number}", flush=True)


# ============================================================
# 增发来源地址状态
# ============================================================

def load_mint_origin_registry() -> dict[str, dict[str, Any]]:
    if not os.path.exists(MINT_ORIGIN_FILE):
        print("没有增发来源地址记录，将从本次扫描开始建立", flush=True)
        return {}

    try:
        with open(MINT_ORIGIN_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        addresses = data.get("addresses", {})
        if not isinstance(addresses, dict):
            raise ValueError("addresses字段格式不正确")

        print(f"已读取增发来源地址：{len(addresses)}个", flush=True)
        return addresses

    except Exception as error:
        print(
            f"读取{MINT_ORIGIN_FILE}失败：{error}，使用空记录",
            flush=True,
        )
        return {}


def save_mint_origin_registry(
    registry: dict[str, dict[str, Any]],
) -> None:
    temporary_file = f"{MINT_ORIGIN_FILE}.tmp"

    data = {
        "version": 1,
        "token": IBS_ADDRESS,
        "addresses": registry,
    }

    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    os.replace(temporary_file, MINT_ORIGIN_FILE)
    print(f"已保存增发来源地址：{len(registry)}个", flush=True)


# ============================================================
# Telegram
# ============================================================

def send_telegram(message: str) -> None:
    if not BOT_TOKEN:
        raise RuntimeError("没有设置GitHub Secret：BOT_TOKEN")

    if not CHAT_ID:
        raise RuntimeError("没有设置GitHub Secret：CHAT_ID")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                url,
                json={
                    "chat_id": CHAT_ID,
                    "text": message,
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )

            if response.ok:
                result = response.json()
                if result.get("ok"):
                    print("Telegram消息发送成功", flush=True)
                    return

            raise RuntimeError(
                f"HTTP {response.status_code}：{response.text[:500]}"
            )

        except Exception as error:
            last_error = error
            print(
                f"Telegram发送失败（{attempt}/{MAX_RETRIES}）：{error}",
                flush=True,
            )

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BASE_SECONDS * attempt)

    raise RuntimeError(f"Telegram连续发送失败：{last_error}")


# ============================================================
# 工具函数
# ============================================================

def normalize_address(address: Any) -> str:
    return str(address).lower()


def topic_to_address(topic: Any) -> str:
    topic_hex = Web3.to_hex(topic)
    return "0x" + topic_hex[-40:]


def address_to_topic(address: str) -> str:
    clean = address.lower().replace("0x", "")
    return "0x" + clean.rjust(64, "0")


def data_to_int(data: Any) -> int:
    if isinstance(data, bytes):
        return int.from_bytes(data, byteorder="big")

    return int(Web3.to_hex(data), 16)


def format_amount(amount: Decimal) -> str:
    text = f"{amount:.6f}"
    return text.rstrip("0").rstrip(".")


def short_address(address: str) -> str:
    if not address:
        return "未知"

    return f"{address[:8]}...{address[-6:]}"


def event_position(event: Any) -> tuple[int, int, int]:
    return (
        int(event["blockNumber"]),
        int(event.get("transactionIndex", 0)),
        int(event.get("logIndex", 0)),
    )


def error_text(error: Exception) -> str:
    return str(error).lower()


def is_rate_limit_error(error: Exception) -> bool:
    text = error_text(error)
    keywords = (
        "429",
        "too many requests",
        "rate limit",
        "compute units",
        "-32005",
        "request limit",
    )
    return any(keyword in text for keyword in keywords)


def is_range_error(error: Exception) -> bool:
    text = error_text(error)
    keywords = (
        "invalid block range",
        "block range",
        "query returned more than",
        "response size exceeded",
        "log response size exceeded",
    )
    return any(keyword in text for keyword in keywords)


# ============================================================
# 解析IBS Transfer
# ============================================================

def parse_ibs_transfers(receipt: Any) -> list[dict[str, Any]]:
    transfers: list[dict[str, Any]] = []

    for log in receipt["logs"]:
        if normalize_address(log["address"]) != IBS_ADDRESS.lower():
            continue

        topics = log["topics"]
        if len(topics) < 3:
            continue

        first_topic = Web3.to_hex(topics[0]).lower()
        if first_topic != TRANSFER_TOPIC:
            continue

        transfers.append(
            {
                "from": topic_to_address(topics[1]).lower(),
                "to": topic_to_address(topics[2]).lower(),
                "amount": data_to_int(log["data"]),
            }
        )

    return transfers


def find_trade_wallet(
    transfers: list[dict[str, Any]],
    trade_type: str,
    fallback_address: str,
) -> str:
    pair_lower = PAIR_ADDRESS.lower()

    if trade_type == "BUY":
        for transfer in transfers:
            if (
                transfer["from"] == pair_lower
                and transfer["to"] not in BURN_ADDRESSES
            ):
                return Web3.to_checksum_address(transfer["to"])

    if trade_type == "SELL":
        for transfer in transfers:
            if (
                transfer["to"] == pair_lower
                and transfer["from"] not in BURN_ADDRESSES
            ):
                return Web3.to_checksum_address(transfer["from"])

    return fallback_address


def get_trade_mark(
    transfers: list[dict[str, Any]],
    wallet: str,
    trade_type: str,
    mint_registry: dict[str, dict[str, Any]],
    ibs_decimals: int,
) -> tuple[str, list[str]]:
    """
    返回：
    1. 标题后缀
    2. Telegram附加说明行
    """

    wallet_lower = wallet.lower()
    divisor = Decimal(10) ** ibs_decimals

    if trade_type == "BUY":
        for transfer in transfers:
            if (
                transfer["from"] == wallet_lower
                and transfer["to"] in BURN_ADDRESSES
            ):
                return "（买入后销毁）", []

        return "", []

    # 卖出：优先检查本笔交易是否直接从零/黑洞地址获得IBS
    direct_origin_raw = sum(
        int(transfer["amount"])
        for transfer in transfers
        if (
            transfer["from"] in BURN_ADDRESSES
            and transfer["to"] == wallet_lower
        )
    )

    if direct_origin_raw > 0:
        direct_amount = Decimal(direct_origin_raw) / divisor
        return (
            "（增发后卖出）",
            [
                "地址标记：⚠️ 增发关联地址（疑似项目方）",
                (
                    "检测依据：本笔交易从零/黑洞地址收到 "
                    f"{format_amount(direct_amount)} IBS 后卖出"
                ),
            ],
        )

    # 检查此前扫描过程中保存的直接增发来源记录
    record = mint_registry.get(wallet_lower)
    if record:
        total_raw = int(record.get("total_raw", 0))
        total_amount = Decimal(total_raw) / divisor
        count = int(record.get("count", 0))
        last_block = int(record.get("last_block", 0))
        source_labels = record.get("sources", [])
        source_text = "、".join(
            short_address(source)
            for source in source_labels
            if isinstance(source, str)
        ) or "零/黑洞地址"

        return (
            "（增发关联地址卖出）",
            [
                "地址标记：⚠️ 增发关联地址（疑似项目方）",
                (
                    f"历史记录：曾直接收到 {format_amount(total_amount)} IBS"
                    f"（{count}笔）"
                ),
                f"来源地址：{source_text}",
                f"最近记录区块：{last_block}",
            ],
        )

    return "", []


# ============================================================
# 查询日志
# ============================================================

def get_swap_events(
    pair_contract: Any,
    from_block: int,
    to_block: int,
) -> list[Any]:
    if from_block > to_block:
        return []

    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            events = pair_contract.events.Swap.get_logs(
                from_block=from_block,
                to_block=to_block,
            )
            return list(events)

        except Exception as error:
            last_error = error

            print(
                f"Swap日志查询失败 {from_block}-{to_block} "
                f"（{attempt}/{MAX_RETRIES}）："
                f"{type(error).__name__}：{error}",
                flush=True,
            )

            if is_range_error(error) and from_block < to_block:
                break

            if attempt < MAX_RETRIES:
                if is_rate_limit_error(error):
                    wait_seconds = RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                else:
                    wait_seconds = RETRY_BASE_SECONDS * attempt

                print(f"等待{wait_seconds}秒后重试……", flush=True)
                time.sleep(wait_seconds)

    if from_block >= to_block:
        raise RuntimeError(
            f"单区块{from_block} Swap日志查询失败：{last_error}"
        )

    middle = (from_block + to_block) // 2

    return (
        get_swap_events(pair_contract, from_block, middle)
        + get_swap_events(pair_contract, middle + 1, to_block)
    )


def get_mint_origin_logs(
    web3: Web3,
    from_block: int,
    to_block: int,
) -> list[Any]:
    """
    查询IBS Transfer日志中：
    ZERO/DEAD -> 任意地址

    与Swap使用同样的10区块范围和自动拆分策略。
    """

    if from_block > to_block:
        return []

    from_topics = [
        address_to_topic(ZERO_ADDRESS),
        address_to_topic(DEAD_ADDRESS),
    ]

    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logs = web3.eth.get_logs(
                {
                    "fromBlock": from_block,
                    "toBlock": to_block,
                    "address": IBS_ADDRESS,
                    "topics": [
                        TRANSFER_TOPIC,
                        from_topics,
                    ],
                }
            )
            return list(logs)

        except Exception as error:
            last_error = error

            print(
                f"增发来源日志查询失败 {from_block}-{to_block} "
                f"（{attempt}/{MAX_RETRIES}）："
                f"{type(error).__name__}：{error}",
                flush=True,
            )

            if is_range_error(error) and from_block < to_block:
                break

            if attempt < MAX_RETRIES:
                if is_rate_limit_error(error):
                    wait_seconds = RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                else:
                    wait_seconds = RETRY_BASE_SECONDS * attempt

                print(f"等待{wait_seconds}秒后重试……", flush=True)
                time.sleep(wait_seconds)

    if from_block >= to_block:
        raise RuntimeError(
            f"单区块{from_block} 增发来源日志查询失败：{last_error}"
        )

    middle = (from_block + to_block) // 2

    return (
        get_mint_origin_logs(web3, from_block, middle)
        + get_mint_origin_logs(web3, middle + 1, to_block)
    )


# ============================================================
# 记录增发来源地址
# ============================================================

def register_mint_origin(
    log: Any,
    registry: dict[str, dict[str, Any]],
) -> None:
    topics = log["topics"]
    if len(topics) < 3:
        return

    source = topic_to_address(topics[1]).lower()
    recipient = topic_to_address(topics[2]).lower()

    if source not in BURN_ADDRESSES:
        return

    # 不记录转回零/黑洞地址的事件
    if recipient in BURN_ADDRESSES:
        return

    amount = data_to_int(log["data"])
    if amount <= 0:
        return

    tx_hash = Web3.to_hex(log["transactionHash"])
    log_index = int(log.get("logIndex", 0))
    event_id = f"{tx_hash}:{log_index}"
    block_number = int(log["blockNumber"])

    record = registry.setdefault(
        recipient,
        {
            "count": 0,
            "total_raw": 0,
            "first_block": block_number,
            "last_block": block_number,
            "first_tx": tx_hash,
            "last_tx": tx_hash,
            "sources": [],
            "event_ids": [],
        },
    )

    event_ids = record.setdefault("event_ids", [])
    if event_id in event_ids:
        return

    record["count"] = int(record.get("count", 0)) + 1
    record["total_raw"] = int(record.get("total_raw", 0)) + amount
    record["first_block"] = min(
        int(record.get("first_block", block_number)),
        block_number,
    )
    record["last_block"] = max(
        int(record.get("last_block", block_number)),
        block_number,
    )

    if block_number <= int(record.get("first_block", block_number)):
        record["first_tx"] = tx_hash

    if block_number >= int(record.get("last_block", block_number)):
        record["last_tx"] = tx_hash

    sources = record.setdefault("sources", [])
    if source not in sources:
        sources.append(source)

    event_ids.append(event_id)

    print(
        "发现零/黑洞地址来源："
        f"{short_address(source)} -> {short_address(recipient)}，"
        f"区块{block_number}",
        flush=True,
    )


# ============================================================
# 处理Swap
# ============================================================

def process_swap(
    web3: Web3,
    event: Any,
    ibs_is_token0: bool,
    ibs_decimals: int,
    notified_hashes: set[str],
    mint_registry: dict[str, dict[str, Any]],
) -> None:
    args = event["args"]

    if ibs_is_token0:
        raw_ibs_in = int(args["amount0In"])
        raw_ibs_out = int(args["amount0Out"])
    else:
        raw_ibs_in = int(args["amount1In"])
        raw_ibs_out = int(args["amount1Out"])

    divisor = Decimal(10) ** ibs_decimals

    if raw_ibs_in > 0:
        trade_type = "SELL"
        raw_amount = raw_ibs_in
        icon = "🔴"
        title = "IBS 大额卖出"
    elif raw_ibs_out > 0:
        trade_type = "BUY"
        raw_amount = raw_ibs_out
        icon = "🟢"
        title = "IBS 大额买入"
    else:
        return

    ibs_amount = Decimal(raw_amount) / divisor

    if ibs_amount < MIN_IBS_AMOUNT:
        print(
            f"忽略小额交易：{trade_type} "
            f"{format_amount(ibs_amount)} IBS",
            flush=True,
        )
        return

    tx_hash = Web3.to_hex(event["transactionHash"])

    if tx_hash in notified_hashes:
        print(f"忽略同交易重复Swap：{tx_hash}", flush=True)
        return

    block_number = int(event["blockNumber"])

    print(
        f"发现大额交易：{trade_type} "
        f"{format_amount(ibs_amount)} IBS",
        flush=True,
    )

    transaction = web3.eth.get_transaction(tx_hash)
    receipt = web3.eth.get_transaction_receipt(tx_hash)
    transfers = parse_ibs_transfers(receipt)

    if trade_type == "BUY":
        fallback_wallet = Web3.to_checksum_address(args["to"])
    else:
        fallback_wallet = Web3.to_checksum_address(transaction["from"])

    wallet = find_trade_wallet(
        transfers=transfers,
        trade_type=trade_type,
        fallback_address=fallback_wallet,
    )

    title_suffix, mark_lines = get_trade_mark(
        transfers=transfers,
        wallet=wallet,
        trade_type=trade_type,
        mint_registry=mint_registry,
        ibs_decimals=ibs_decimals,
    )

    detail_lines = [
        f"数量：{format_amount(ibs_amount)} IBS",
        f"钱包：{wallet}",
    ]

    detail_lines.extend(mark_lines)
    detail_lines.append(f"区块：{block_number}")

    message = (
        f"{icon} {title}{title_suffix}\n\n"
        + "\n".join(detail_lines)
        + "\n\n"
        + f"地址：https://bscscan.com/address/{wallet}\n"
        + f"交易：https://bscscan.com/tx/{tx_hash}"
    )

    print(message, flush=True)
    send_telegram(message)
    notified_hashes.add(tx_hash)


# ============================================================
# 主程序
# ============================================================

def main() -> None:
    start_time = time.monotonic()

    print("IBS监控程序启动", flush=True)

    if not BOT_TOKEN:
        raise RuntimeError("缺少GitHub Secret：BOT_TOKEN")

    if not CHAT_ID:
        raise RuntimeError("缺少GitHub Secret：CHAT_ID")

    if not BSC_RPC:
        raise RuntimeError("缺少GitHub Secret：BSC_RPC")

    web3 = connect_web3()

    pair_contract = web3.eth.contract(
        address=PAIR_ADDRESS,
        abi=PAIR_ABI,
    )
    ibs_contract = web3.eth.contract(
        address=IBS_ADDRESS,
        abi=TOKEN_ABI,
    )

    print("正在读取Pair信息……", flush=True)

    token0 = Web3.to_checksum_address(
        pair_contract.functions.token0().call()
    )
    token1 = Web3.to_checksum_address(
        pair_contract.functions.token1().call()
    )

    print(f"token0：{token0}", flush=True)
    print(f"token1：{token1}", flush=True)

    if IBS_ADDRESS == token0:
        ibs_is_token0 = True
    elif IBS_ADDRESS == token1:
        ibs_is_token0 = False
    else:
        raise RuntimeError(
            "Pair中没有找到IBS代币，请检查PAIR_ADDRESS"
        )

    ibs_decimals = int(
        ibs_contract.functions.decimals().call()
    )

    try:
        symbol = ibs_contract.functions.symbol().call()
    except Exception:
        symbol = "IBS"

    print(
        f"代币：{symbol}，decimals：{ibs_decimals}，"
        f"IBS为token{'0' if ibs_is_token0 else '1'}",
        flush=True,
    )

    latest_block = int(web3.eth.block_number)
    network_safe_latest = max(
        1,
        latest_block - CONFIRMATION_BLOCKS,
    )

    print(
        f"安全扫描区块：{network_safe_latest} "
        f"（落后最新区块{CONFIRMATION_BLOCKS}块）",
        flush=True,
    )

    last_block = read_last_block(network_safe_latest)
    current_block = last_block + 1

    if current_block > network_safe_latest:
        print("目前没有新区块需要扫描", flush=True)
        return

    run_target_block = min(
        network_safe_latest,
        last_block + MAX_BLOCKS_PER_RUN,
    )

    pending_blocks = network_safe_latest - last_block

    print(f"当前待扫描区块数量：{pending_blocks}", flush=True)
    print(
        f"本次扫描范围：{current_block}-{run_target_block}",
        flush=True,
    )

    mint_registry = load_mint_origin_registry()
    notified_hashes: set[str] = set()

    while current_block <= run_target_block:
        elapsed_seconds = time.monotonic() - start_time

        if elapsed_seconds >= MAX_RUNTIME_SECONDS:
            print(
                "接近运行时间上限，主动结束并保留当前进度",
                flush=True,
            )
            break

        end_block = min(
            current_block + BLOCK_CHUNK_SIZE - 1,
            run_target_block,
        )

        print(
            f"正在扫描：{current_block}-{end_block}",
            flush=True,
        )

        swap_events = get_swap_events(
            pair_contract,
            current_block,
            end_block,
        )

        mint_logs = get_mint_origin_logs(
            web3,
            current_block,
            end_block,
        )

        print(f"发现Swap：{len(swap_events)}", flush=True)
        print(f"发现零/黑洞来源Transfer：{len(mint_logs)}", flush=True)

        # 按区块、交易顺序、日志顺序处理。
        # 这样同一区块中“先增发、后卖出”也能正确标记，
        # 不会把卖出之后才发生的增发错误算到前面的卖出上。
        timeline: list[tuple[tuple[int, int, int], str, Any]] = []

        for log in mint_logs:
            timeline.append((event_position(log), "MINT", log))

        for event in swap_events:
            timeline.append((event_position(event), "SWAP", event))

        timeline.sort(key=lambda item: item[0])

        for _, event_type, item in timeline:
            if event_type == "MINT":
                register_mint_origin(
                    log=item,
                    registry=mint_registry,
                )
            else:
                process_swap(
                    web3=web3,
                    event=item,
                    ibs_is_token0=ibs_is_token0,
                    ibs_decimals=ibs_decimals,
                    notified_hashes=notified_hashes,
                    mint_registry=mint_registry,
                )

        # 这一批全部处理成功后保存两个状态文件
        save_mint_origin_registry(mint_registry)
        save_last_block(end_block)

        current_block = end_block + 1

    completed_block = current_block - 1

    if completed_block >= network_safe_latest:
        print("已追赶到当前安全区块", flush=True)
    else:
        remaining_blocks = network_safe_latest - completed_block
        print(
            f"仍有{remaining_blocks}个区块待扫描，下一次运行继续",
            flush=True,
        )

    print("本次监控完成", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"程序运行失败：{type(error).__name__}：{error}",
            flush=True,
        )
        sys.exit(1)
