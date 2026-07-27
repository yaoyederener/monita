import os
import sys
import time
from decimal import Decimal
from typing import Any

import requests
from web3 import Web3


# ============================================================
# 配置
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

MIN_IBS_AMOUNT = Decimal("50")
LAST_BLOCK_FILE = "last_block.txt"

# 第一次运行检查最近500个区块
FIRST_RUN_BLOCKS = 500

# 每次查询50个区块，减少免费RPC压力
BLOCK_CHUNK_SIZE = 50

# 等待3个确认区块
CONFIRMATION_BLOCKS = 3

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
CUSTOM_RPC = os.environ.get("BSC_RPC", "").strip()

# 免费RPC可能随时限流，因此准备多个备用节点
RPC_LIST = [
    CUSTOM_RPC,
    "https://bsc.drpc.org",
    "https://1rpc.io/bnb",
    "https://bsc-mainnet.public.blastapi.io",
]

RPC_LIST = list(dict.fromkeys(rpc for rpc in RPC_LIST if rpc))

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
]

TOKEN_ABI = [
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
]


# ============================================================
# RPC连接
# ============================================================

def create_web3(rpc_url: str) -> Web3:
    return Web3(
        Web3.HTTPProvider(
            rpc_url,
            request_kwargs={"timeout": 12},
        )
    )


def connect_web3(
    excluded_urls: set[str] | None = None,
) -> tuple[Web3, str]:
    """
    寻找能正常使用eth_getLogs的BSC RPC。

    不只是测试最新区块，还会实际执行一次日志查询，
    避免出现“能连接但查询日志403”的情况。
    """

    excluded_urls = excluded_urls or set()
    last_error: Exception | None = None

    for rpc_url in RPC_LIST:
        if rpc_url in excluded_urls:
            continue

        print(f"正在测试RPC：{rpc_url}", flush=True)

        try:
            web3 = create_web3(rpc_url)

            if not web3.is_connected():
                print("RPC无法连接", flush=True)
                continue

            chain_id = web3.eth.chain_id

            if chain_id != 56:
                print(
                    f"Chain ID错误：{chain_id}",
                    flush=True,
                )
                continue

            latest_block = web3.eth.block_number

            # 关键：实际测试eth_getLogs
            web3.eth.get_logs(
                {
                    "fromBlock": latest_block,
                    "toBlock": latest_block,
                    "address": PAIR_ADDRESS,
                }
            )

            print(
                f"RPC可用：{rpc_url}",
                flush=True,
            )
            print(
                f"BSC最新区块：{latest_block}",
                flush=True,
            )

            return web3, rpc_url

        except Exception as error:
            last_error = error

            print(
                f"RPC不可用：{error}",
                flush=True,
            )

    raise RuntimeError(
        "所有RPC均不可用或不支持eth_getLogs。"
        f"最后错误：{last_error}"
    )


# ============================================================
# 区块进度
# ============================================================

def read_last_block(latest_block: int) -> int:
    if not os.path.exists(LAST_BLOCK_FILE):
        start_block = max(
            1,
            latest_block - FIRST_RUN_BLOCKS,
        )

        print(
            f"没有last_block.txt，从{start_block}开始",
            flush=True,
        )

        return start_block - 1

    try:
        with open(
            LAST_BLOCK_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            text = file.read().strip()

        last_block = int(text)

        if last_block <= 0:
            start_block = max(
                1,
                latest_block - FIRST_RUN_BLOCKS,
            )

            print(
                f"首次运行，从{start_block}开始",
                flush=True,
            )

            return start_block - 1

        if last_block > latest_block:
            raise ValueError(
                "last_block高于当前BSC区块"
            )

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

        start_block = max(
            1,
            latest_block - FIRST_RUN_BLOCKS,
        )

        return start_block - 1


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

    response = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=15,
    )

    if not response.ok:
        raise RuntimeError(
            "Telegram发送失败："
            f"{response.status_code} "
            f"{response.text}"
        )

    print("Telegram发送成功", flush=True)


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

    return int(Web3.to_hex(data), 16)


def format_amount(amount: Decimal) -> str:
    text = f"{amount:.6f}"
    return text.rstrip("0").rstrip(".")


def short_address(address: str) -> str:
    if not address:
        return "未知"

    return f"{address[:8]}...{address[-6:]}"


# ============================================================
# 解析IBS Transfer
# ============================================================

def parse_ibs_transfers(
    receipt: Any,
) -> list[dict[str, Any]]:
    """
    只解析当前交易回执中的IBS Transfer事件。

    不扫描全链Transfer，所以RPC请求量很小。
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
    pair_address = PAIR_ADDRESS.lower()

    if trade_type == "BUY":
        # 买入：Pair -> 用户
        for transfer in transfers:
            if (
                transfer["from"] == pair_address
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
                transfer["to"] == pair_address
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
    wallet_lower = wallet.lower()

    if trade_type == "BUY":
        # 用户买入后，同一笔交易把IBS转入黑洞
        for transfer in transfers:
            if (
                transfer["from"] == wallet_lower
                and transfer["to"]
                in BURN_ADDRESSES
            ):
                return "（买入后已销毁）"

    if trade_type == "SELL":
        # 同一笔交易中，零地址先增发IBS给用户
        for transfer in transfers:
            if (
                transfer["from"]
                == ZERO_ADDRESS.lower()
                and transfer["to"]
                == wallet_lower
            ):
                return "（来自增发）"

    return ""


# ============================================================
# 查询Swap日志
# ============================================================

def is_rpc_rejection(error: Exception) -> bool:
    error_text = str(error).lower()

    rejection_words = [
        "403",
        "forbidden",
        "401",
        "unauthorized",
        "429",
        "too many requests",
        "rate limit",
        "method disabled",
        "method not allowed",
        "not supported",
        "access denied",
    ]

    return any(
        word in error_text
        for word in rejection_words
    )


def is_range_error(error: Exception) -> bool:
    error_text = str(error).lower()

    range_words = [
        "limit exceeded",
        "-32005",
        "query returned more",
        "response size exceeded",
        "block range",
        "too many results",
        "range is too wide",
        "please limit",
    ]

    return any(
        word in error_text
        for word in range_words
    )


def get_logs_with_split(
    pair_contract: Any,
    from_block: int,
    to_block: int,
) -> list[Any]:
    """
    获取Swap日志。

    403、429等错误直接交给主程序切换RPC。
    只有区块范围过大时才拆分查询。
    """

    try:
        return list(
            pair_contract.events.Swap.get_logs(
                from_block=from_block,
                to_block=to_block,
            )
        )

    except Exception as error:
        if is_rpc_rejection(error):
            raise RuntimeError(
                f"RPC拒绝日志查询：{error}"
            ) from error

        if not is_range_error(error):
            raise RuntimeError(
                f"日志查询失败：{error}"
            ) from error

        if from_block >= to_block:
            raise RuntimeError(
                f"单区块{from_block}查询失败：{error}"
            ) from error

        middle = (
            from_block + to_block
        ) // 2

        print(
            "区块范围过大，拆分为："
            f"{from_block}-{middle} 和 "
            f"{middle + 1}-{to_block}",
            flush=True,
        )

        left_logs = get_logs_with_split(
            pair_contract,
            from_block,
            middle,
        )

        right_logs = get_logs_with_split(
            pair_contract,
            middle + 1,
            to_block,
        )

        return left_logs + right_logs


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

    divisor = (
        Decimal(10) ** ibs_decimals
    )

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

    # 防止同一运行中重复通知
    if tx_hash in notified_hashes:
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
        fallback_wallet = (
            Web3.to_checksum_address(
                args["to"]
            )
        )
    else:
        fallback_wallet = (
            Web3.to_checksum_address(
                transaction["from"]
            )
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
# 创建合约
# ============================================================

def create_contracts(
    web3: Web3,
) -> tuple[Any, Any]:
    pair_contract = web3.eth.contract(
        address=PAIR_ADDRESS,
        abi=PAIR_ABI,
    )

    ibs_contract = web3.eth.contract(
        address=IBS_ADDRESS,
        abi=TOKEN_ABI,
    )

    return pair_contract, ibs_contract


# ============================================================
# 主程序
# ============================================================

def main() -> None:
    print(
        "IBS监控程序启动",
        flush=True,
    )

    if not BOT_TOKEN:
        raise RuntimeError(
            "缺少GitHub Secret：BOT_TOKEN"
        )

    if not CHAT_ID:
        raise RuntimeError(
            "缺少GitHub Secret：CHAT_ID"
        )

    failed_rpcs: set[str] = set()

    web3, current_rpc = connect_web3(
        failed_rpcs
    )

    pair_contract, ibs_contract = (
        create_contracts(web3)
    )

    print(
        "正在读取Pair信息……",
        flush=True,
    )

    token0 = Web3.to_checksum_address(
        pair_contract.functions.token0().call()
    )

    token1 = Web3.to_checksum_address(
        pair_contract.functions.token1().call()
    )

    print(
        f"token0：{token0}",
        flush=True,
    )

    print(
        f"token1：{token1}",
        flush=True,
    )

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

        try:
            events = get_logs_with_split(
                pair_contract,
                current_block,
                end_block,
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

            # 只有整个区间处理完成才保存进度
            save_last_block(end_block)

            current_block = (
                end_block + 1
            )

        except Exception as error:
            print(
                f"当前RPC扫描失败：{error}",
                flush=True,
            )

            failed_rpcs.add(current_rpc)

            if len(failed_rpcs) >= len(
                RPC_LIST
            ):
                raise RuntimeError(
                    "所有RPC均已失败，"
                    "请稍后重试或设置BSC_RPC Secret"
                ) from error

            print(
                "切换下一个RPC……",
                flush=True,
            )

            time.sleep(2)

            web3, current_rpc = (
                connect_web3(failed_rpcs)
            )

            pair_contract, _ = (
                create_contracts(web3)
            )

            # 不增加current_block，
            # 使用新RPC重新扫描当前区间

    print(
        "本次监控完成",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        print(
            f"程序运行失败：{error}",
            flush=True,
        )

        sys.exit(1)
