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
BURN_ADDRESSES = {
    ZERO_ADDRESS.lower(),
    DEAD_ADDRESS.lower(),
}

# 达到50 IBS才发送Telegram通知
MIN_IBS_AMOUNT = Decimal("50")

# 区块进度文件
LAST_BLOCK_FILE = "last_block.txt"

# 等待20个确认区块，避免RPC最新区块与日志索引短暂不同步
CONFIRMATION_BLOCKS = 20

# Alchemy BSC日志查询每次保持10个区块
BLOCK_CHUNK_SIZE = 10

# 每次运行最多扫描1000个区块
MAX_BLOCKS_PER_RUN = 1000

# 最长运行330秒，给GitHub Actions提交进度预留时间
MAX_RUNTIME_SECONDS = 330

# RPC和Telegram重试次数
MAX_RETRIES = 5

# 重试基础等待时间
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

    # 这里不再用最新区块执行eth_getLogs测试。
    # RPC的区块高度可能已经更新，但日志索引可能短暂落后，
    # 直接查询最新区块会触发 invalid block range params。
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
        with open(
            LAST_BLOCK_FILE,
            "r",
            encoding="utf-8",
        ) as file:
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

    with open(
        temporary_file,
        "w",
        encoding="utf-8",
    ) as file:
        file.write(str(block_number))

    os.replace(temporary_file, LAST_BLOCK_FILE)
    print(f"已保存区块：{block_number}", flush=True)


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


def get_special_label(
    transfers: list[dict[str, Any]],
    wallet: str,
    trade_type: str,
) -> str:
    wallet_lower = wallet.lower()

    if trade_type == "BUY":
        for transfer in transfers:
            if (
                transfer["from"] == wallet_lower
                and transfer["to"] in BURN_ADDRESSES
            ):
                return "（买入后销毁）"

    if trade_type == "SELL":
        for transfer in transfers:
            if (
                transfer["from"] == ZERO_ADDRESS.lower()
                and transfer["to"] == wallet_lower
            ):
                return "（增发后卖出）"

    return ""


# ============================================================
# 查询Swap事件
# ============================================================


def get_swap_events(
    pair_contract: Any,
    from_block: int,
    to_block: int,
) -> list[Any]:
    """
    查询Pair的Swap事件。

    - 429和RPC限流：等待后重试
    - 区块范围错误：自动拆分区间
    - 临时网络错误：等待后重试
    """

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
                f"日志查询失败 {from_block}-{to_block} "
                f"（{attempt}/{MAX_RETRIES}）："
                f"{type(error).__name__}：{error}",
                flush=True,
            )

            # 多区块范围错误直接拆分，避免无意义等待
            if is_range_error(error) and from_block < to_block:
                break

            # 单区块的invalid block range通常是RPC日志索引暂时落后
            if is_range_error(error) and from_block == to_block:
                if attempt < MAX_RETRIES:
                    wait_seconds = RETRY_BASE_SECONDS * attempt
                    print(
                        f"单区块日志索引可能延迟，等待{wait_seconds}秒……",
                        flush=True,
                    )
                    time.sleep(wait_seconds)
                    continue

            if attempt < MAX_RETRIES:
                if is_rate_limit_error(error):
                    wait_seconds = RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                else:
                    wait_seconds = RETRY_BASE_SECONDS * attempt

                print(
                    f"等待{wait_seconds}秒后重试……",
                    flush=True,
                )
                time.sleep(wait_seconds)

    if from_block >= to_block:
        raise RuntimeError(
            f"单区块{from_block}日志查询失败：{last_error}"
        )

    middle = (from_block + to_block) // 2

    print(
        "查询失败，自动拆分："
        f"{from_block}-{middle}，"
        f"{middle + 1}-{to_block}",
        flush=True,
    )

    left_events = get_swap_events(
        pair_contract,
        from_block,
        middle,
    )
    right_events = get_swap_events(
        pair_contract,
        middle + 1,
        to_block,
    )

    return left_events + right_events


# ============================================================
# 处理Swap
# ============================================================


def process_swap(
    web3: Web3,
    event: Any,
    ibs_is_token0: bool,
    ibs_decimals: int,
    notified_hashes: set[str],
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

    special_label = get_special_label(
        transfers=transfers,
        wallet=wallet,
        trade_type=trade_type,
    )

    message = (
        f"{icon} {title}{special_label}\n\n"
        f"数量：{format_amount(ibs_amount)} IBS\n"
        f"钱包：{short_address(wallet)}\n"
        f"区块：{block_number}\n\n"
        f"https://bscscan.com/tx/{tx_hash}"
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

    print(
        f"当前待扫描区块数量：{pending_blocks}",
        flush=True,
    )
    print(
        f"本次扫描范围：{current_block}-{run_target_block}",
        flush=True,
    )

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

        events = get_swap_events(
            pair_contract,
            current_block,
            end_block,
        )

        print(f"发现Swap：{len(events)}", flush=True)

        for event in events:
            process_swap(
                web3=web3,
                event=event,
                ibs_is_token0=ibs_is_token0,
                ibs_decimals=ibs_decimals,
                notified_hashes=notified_hashes,
            )

        # 这一批全部处理成功后，才保存进度
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
