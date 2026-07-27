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
    "0x255e746abb8d9acac00d6d023e5e63e3b8dfa7cd"
)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DEAD_ADDRESS = "0x000000000000000000000000000000000000dead"

BURN_ADDRESSES = {
    ZERO_ADDRESS.lower(),
    DEAD_ADDRESS.lower(),
}

# 只有达到50 IBS才通知
MIN_IBS_AMOUNT = Decimal("50")

LAST_BLOCK_FILE = "last_block.txt"

# 等待3个确认区块，降低区块重组影响
CONFIRMATION_BLOCKS = 3

# 每次查询最多200个区块
BLOCK_CHUNK_SIZE = 200

# RPC和Telegram重试次数
MAX_RETRIES = 3

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
        raise RuntimeError(
            "没有设置GitHub Secret：BSC_RPC"
        )

    print("正在连接Alchemy BSC RPC……", flush=True)

    web3 = Web3(
        Web3.HTTPProvider(
            BSC_RPC,
            request_kwargs={"timeout": 20},
        )
    )

    if not web3.is_connected():
        raise RuntimeError(
            "Alchemy RPC连接失败，请检查BSC_RPC"
        )

    chain_id = web3.eth.chain_id

    if chain_id != 56:
        raise RuntimeError(
            f"网络错误，当前Chain ID为{chain_id}，"
            "BSC Mainnet应为56"
        )

    latest_block = web3.eth.block_number

    # 实际测试Pair日志查询功能
    web3.eth.get_logs(
        {
            "fromBlock": latest_block,
            "toBlock": latest_block,
            "address": PAIR_ADDRESS,
        }
    )

    print("Alchemy RPC连接成功", flush=True)
    print(f"BSC最新区块：{latest_block}", flush=True)

    return web3


# ============================================================
# 区块进度
# ============================================================

def read_last_block(safe_latest: int) -> int:
    """
    last_block.txt为0时，从当前区块开始。

    第一次运行不会补发大量历史交易。
    """

    if not os.path.exists(LAST_BLOCK_FILE):
        print(
            "没有last_block.txt，从当前区块开始",
            flush=True,
        )
        return safe_latest - 1

    try:
        with open(
            LAST_BLOCK_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            content = file.read().strip()

        last_block = int(content)

        if last_block <= 0:
            print(
                "首次运行，不补发历史交易",
                flush=True,
            )
            return safe_latest - 1

        if last_block > safe_latest:
            print(
                "last_block高于当前安全区块，"
                "从当前安全区块继续",
                flush=True,
            )
            return safe_latest - 1

        print(
            f"上次扫描到区块：{last_block}",
            flush=True,
        )

        return last_block

    except Exception as error:
        print(
            f"读取last_block.txt失败：{error}",
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

    os.replace(
        temporary_file,
        LAST_BLOCK_FILE,
    )

    print(
        f"已保存区块：{block_number}",
        flush=True,
    )


# ============================================================
# Telegram
# ============================================================

def send_telegram(message: str) -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "没有设置GitHub Secret：BOT_TOKEN"
        )

    if not CHAT_ID:
        raise RuntimeError(
            "没有设置GitHub Secret：CHAT_ID"
        )

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

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
                print(
                    "Telegram消息发送成功",
                    flush=True,
                )
                return

            raise RuntimeError(
                f"HTTP {response.status_code}："
                f"{response.text}"
            )

        except Exception as error:
            last_error = error

            print(
                f"Telegram发送失败"
                f"（{attempt}/{MAX_RETRIES}）：{error}",
                flush=True,
            )

            if attempt < MAX_RETRIES:
                time.sleep(2)

    raise RuntimeError(
        f"Telegram连续发送失败：{last_error}"
    )


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
        return int.from_bytes(
            data,
            byteorder="big",
        )

    return int(
        Web3.to_hex(data),
        16,
    )


def format_amount(amount: Decimal) -> str:
    text = f"{amount:.6f}"
    return text.rstrip("0").rstrip(".")


def short_address(address: str) -> str:
    if not address:
        return "未知"

    return (
        f"{address[:8]}..."
        f"{address[-6:]}"
    )


# ============================================================
# 解析IBS Transfer
# ============================================================

def parse_ibs_transfers(
    receipt: Any,
) -> list[dict[str, Any]]:
    """
    只解析当前交易回执中的IBS Transfer事件。

    不扫描全链Transfer，因此RPC请求量很小。
    """

    transfers: list[dict[str, Any]] = []

    for log in receipt["logs"]:
        if (
            normalize_address(log["address"])
            != IBS_ADDRESS.lower()
        ):
            continue

        topics = log["topics"]

        if len(topics) < 3:
            continue

        first_topic = Web3.to_hex(
            topics[0]
        ).lower()

        if first_topic != TRANSFER_TOPIC:
            continue

        transfers.append(
            {
                "from": topic_to_address(
                    topics[1]
                ).lower(),
                "to": topic_to_address(
                    topics[2]
                ).lower(),
                "amount": data_to_int(
                    log["data"]
                ),
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
        # 买入：Pair -> 用户
        for transfer in transfers:
            if (
                transfer["from"] == pair_lower
                and transfer["to"]
                not in BURN_ADDRESSES
            ):
                return Web3.to_checksum_address(
                    transfer["to"]
                )

    if trade_type == "SELL":
        # 卖出：用户 -> Pair
        for transfer in transfers:
            if (
                transfer["to"] == pair_lower
                and transfer["from"]
                not in BURN_ADDRESSES
            ):
                return Web3.to_checksum_address(
                    transfer["from"]
                )

    return fallback_address


def get_special_label(
    transfers: list[dict[str, Any]],
    wallet: str,
    trade_type: str,
) -> str:
    """
    检查同一笔交易里的特殊动作。

    买入：
    Pair -> 用户 -> 黑洞

    卖出：
    零地址 -> 用户 -> Pair
    """

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

    如果较大的区块范围失败，会自动拆分。
    """

    try:
        return list(
            pair_contract.events.Swap.get_logs(
                from_block=from_block,
                to_block=to_block,
            )
        )

    except Exception as error:
        if from_block >= to_block:
            raise RuntimeError(
                f"单区块{from_block}日志查询失败：{error}"
            ) from error

        middle = (
            from_block + to_block
        ) // 2

        print(
            "日志查询失败，自动拆分："
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
        raw_ibs_in = int(
            args["amount0In"]
        )
        raw_ibs_out = int(
            args["amount0Out"]
        )
    else:
        raw_ibs_in = int(
            args["amount1In"]
        )
        raw_ibs_out = int(
            args["amount1Out"]
        )

    divisor = Decimal(10) ** ibs_decimals

    # IBS进入池子 = 卖出
    if raw_ibs_in > 0:
        trade_type = "SELL"
        raw_amount = raw_ibs_in
        icon = "🔴"
        title = "IBS 大额卖出"

    # IBS离开池子 = 买入
    elif raw_ibs_out > 0:
        trade_type = "BUY"
        raw_amount = raw_ibs_out
        icon = "🟢"
        title = "IBS 大额买入"

    else:
        return

    ibs_amount = (
        Decimal(raw_amount) / divisor
    )

    if ibs_amount < MIN_IBS_AMOUNT:
        print(
            f"忽略小额交易："
            f"{trade_type} "
            f"{format_amount(ibs_amount)} IBS",
            flush=True,
        )
        return

    tx_hash = Web3.to_hex(
        event["transactionHash"]
    )

    # 同一笔交易只通知一次
    if tx_hash in notified_hashes:
        print(
            f"忽略同交易重复Swap：{tx_hash}",
            flush=True,
        )
        return

    block_number = int(
        event["blockNumber"]
    )

    print(
        f"发现大额交易："
        f"{trade_type} "
        f"{format_amount(ibs_amount)} IBS",
        flush=True,
    )

    transaction = web3.eth.get_transaction(
        tx_hash
    )

    receipt = (
        web3.eth.get_transaction_receipt(
            tx_hash
        )
    )

    transfers = parse_ibs_transfers(
        receipt
    )

    if trade_type == "BUY":
        fallback_wallet = Web3.to_checksum_address(
            args["to"]
        )
    else:
        fallback_wallet = Web3.to_checksum_address(
            transaction["from"]
        )

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
    print("IBS监控程序启动", flush=True)

    if not BOT_TOKEN:
        raise RuntimeError(
            "缺少GitHub Secret：BOT_TOKEN"
        )

    if not CHAT_ID:
        raise RuntimeError(
            "缺少GitHub Secret：CHAT_ID"
        )

    if not BSC_RPC:
        raise RuntimeError(
            "缺少GitHub Secret：BSC_RPC"
        )

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
            "Pair中没有找到IBS代币，"
            "请检查PAIR_ADDRESS"
        )

    ibs_decimals = int(
        ibs_contract.functions.decimals().call()
    )

    try:
        symbol = (
            ibs_contract.functions.symbol().call()
        )
    except Exception:
        symbol = "IBS"

    print(
        f"代币：{symbol}，"
        f"decimals：{ibs_decimals}，"
        f"IBS为token"
        f"{'0' if ibs_is_token0 else '1'}",
        flush=True,
    )

    latest_block = web3.eth.block_number

    safe_latest = max(
        1,
        latest_block - CONFIRMATION_BLOCKS,
    )

    last_block = read_last_block(
        safe_latest
    )

    current_block = last_block + 1

    if current_block > safe_latest:
        print(
            "目前没有新区块需要扫描",
            flush=True,
        )
        return

    print(
        f"扫描范围："
        f"{current_block}-{safe_latest}",
        flush=True,
    )

    notified_hashes: set[str] = set()

    while current_block <= safe_latest:
        end_block = min(
            current_block
            + BLOCK_CHUNK_SIZE
            - 1,
            safe_latest,
        )

        print(
            f"正在扫描："
            f"{current_block}-{end_block}",
            flush=True,
        )

        events: list[Any] | None = None
        last_error: Exception | None = None

        for attempt in range(
            1,
            MAX_RETRIES + 1,
        ):
            try:
                events = get_swap_events(
                    pair_contract,
                    current_block,
                    end_block,
                )

                break

            except Exception as error:
                last_error = error

                print(
                    f"Swap日志查询失败"
                    f"（{attempt}/{MAX_RETRIES}）："
                    f"{error}",
                    flush=True,
                )

                if attempt < MAX_RETRIES:
                    time.sleep(3)

        if events is None:
            raise RuntimeError(
                f"区块{current_block}-{end_block}"
                f"连续查询失败：{last_error}"
            )

        print(
            f"发现Swap：{len(events)}",
            flush=True,
        )

        for event in events:
            process_swap(
                web3=web3,
                event=event,
                ibs_is_token0=ibs_is_token0,
                ibs_decimals=ibs_decimals,
                notified_hashes=notified_hashes,
            )

        # 整个范围成功完成后才保存进度
        save_last_block(end_block)

        current_block = end_block + 1

    print("本次监控完成", flush=True)


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        print(
            f"程序运行失败：{error}",
            flush=True,
        )

        sys.exit(1)
