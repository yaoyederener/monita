import os
import sys
import time
from decimal import Decimal
from typing import Any

import requests
from web3 import Web3


# =========================================================
# 配置
# =========================================================

PAIR_ADDRESS = Web3.to_checksum_address(
    "0x2a4B99A9c4544D35e8D266111c50B67fEA01d53d"
)

IBS_ADDRESS = Web3.to_checksum_address(
    "0x255e746abb8d9acac00d6d023e5e63e3b8dfa7cd"
)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DEAD_ADDRESS = "0x000000000000000000000000000000000000dead"
BURN_ADDRESSES = {ZERO_ADDRESS.lower(), DEAD_ADDRESS.lower()}

MIN_IBS_AMOUNT = Decimal("50")
LAST_BLOCK_FILE = "last_block.txt"

# 第一次运行，只检查最近500个区块
FIRST_RUN_BLOCKS = 500

# 每次先按100个区块查询；遇到限制会自动拆小
BLOCK_CHUNK_SIZE = 100

# 等待3个确认区块，减少区块重组影响
CONFIRMATION_BLOCKS = 3

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()

# 可通过 GitHub Secret BSC_RPC 自定义节点。
# 没设置时使用下面的免费公共节点。
CUSTOM_RPC = os.environ.get("BSC_RPC", "").strip()

RPC_LIST = [
    CUSTOM_RPC,
    "https://bsc-rpc.publicnode.com",
    "https://1rpc.io/bnb",
]

RPC_LIST = [rpc for rpc in RPC_LIST if rpc]

# ERC-20 Transfer事件主题
TRANSFER_TOPIC = Web3.keccak(
    text="Transfer(address,address,uint256)"
).hex()


# =========================================================
# 最小ABI
# =========================================================

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


# =========================================================
# RPC
# =========================================================

def connect_web3() -> Web3:
    """自动尝试所有RPC节点。"""

    for rpc_url in RPC_LIST:
        print(f"正在连接RPC：{rpc_url}", flush=True)

        try:
            web3 = Web3(
                Web3.HTTPProvider(
                    rpc_url,
                    request_kwargs={"timeout": 12},
                )
            )

            if not web3.is_connected():
                print("连接失败", flush=True)
                continue

            chain_id = web3.eth.chain_id

            if chain_id != 56:
                print(
                    f"Chain ID错误：{chain_id}",
                    flush=True,
                )
                continue

            latest = web3.eth.block_number

            print(
                f"RPC连接成功，最新区块：{latest}",
                flush=True,
            )
            return web3

        except Exception as error:
            print(
                f"RPC连接异常：{error}",
                flush=True,
            )

    raise RuntimeError("所有免费BSC RPC均无法连接")


# =========================================================
# 区块进度
# =========================================================

def read_last_block(latest_block: int) -> int:
    """读取上次成功扫描的区块号。"""

    if not os.path.exists(LAST_BLOCK_FILE):
        start = max(1, latest_block - FIRST_RUN_BLOCKS)
        print(
            f"没有last_block.txt，从{start}开始",
            flush=True,
        )
        return start - 1

    try:
        with open(
            LAST_BLOCK_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            text = file.read().strip()

        last_block = int(text)

        if last_block <= 0:
            start = max(
                1,
                latest_block - FIRST_RUN_BLOCKS,
            )
            print(
                f"首次运行，从{start}开始",
                flush=True,
            )
            return start - 1

        if last_block > latest_block:
            raise ValueError("保存的区块高于当前区块")

        return last_block

    except Exception as error:
        print(
            f"读取last_block失败：{error}",
            flush=True,
        )

        start = max(
            1,
            latest_block - FIRST_RUN_BLOCKS,
        )
        return start - 1


def save_last_block(block_number: int) -> None:
    """安全保存扫描进度。"""

    temporary_file = LAST_BLOCK_FILE + ".tmp"

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


# =========================================================
# Telegram
# =========================================================

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


# =========================================================
# 工具函数
# =========================================================

def normalize_address(address: Any) -> str:
    return str(address).lower()


def topic_to_address(topic: Any) -> str:
    """从日志topic中提取地址。"""

    topic_hex = Web3.to_hex(topic)
    return "0x" + topic_hex[-40:]


def data_to_int(data: Any) -> int:
    """把日志data转换成整数。"""

    if isinstance(data, bytes):
        return int.from_bytes(data, byteorder="big")

    data_hex = Web3.to_hex(data)
    return int(data_hex, 16)


def format_amount(amount: Decimal) -> str:
    text = f"{amount:.6f}"
    return text.rstrip("0").rstrip(".")


def short_address(address: str) -> str:
    if not address or address == "未知":
        return "未知"

    return f"{address[:8]}...{address[-6:]}"


# =========================================================
# 解析IBS Transfer
# =========================================================

def parse_ibs_transfers(receipt: Any) -> list[dict[str, Any]]:
    """只解析当前交易回执中的IBS Transfer日志。"""

    transfers = []

    for log in receipt["logs"]:
        log_address = normalize_address(log["address"])

        if log_address != IBS_ADDRESS.lower():
            continue

        topics = log["topics"]

        if len(topics) < 3:
            continue

        if Web3.to_hex(topics[0]).lower() != TRANSFER_TOPIC.lower():
            continue

        from_address = topic_to_address(topics[1]).lower()
        to_address = topic_to_address(topics[2]).lower()
        amount = data_to_int(log["data"])

        transfers.append(
            {
                "from": from_address,
                "to": to_address,
                "amount": amount,
            }
        )

    return transfers


def find_trade_wallet(
    transfers: list[dict[str, Any]],
    trade_type: str,
    fallback_address: str,
) -> str:
    """从IBS Transfer中确定实际交易钱包。"""

    pair = PAIR_ADDRESS.lower()

    if trade_type == "BUY":
        # 买入：Pair -> 钱包
        for transfer in transfers:
            if (
                transfer["from"] == pair
                and transfer["to"] not in BURN_ADDRESSES
            ):
                return Web3.to_checksum_address(
                    transfer["to"]
                )

    if trade_type == "SELL":
        # 卖出：钱包 -> Pair
        for transfer in transfers:
            if (
                transfer["to"] == pair
                and transfer["from"] not in BURN_ADDRESSES
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
    """判断买入后销毁或增发后卖出。"""

    wallet_lower = normalize_address(wallet)

    if trade_type == "BUY":
        for transfer in transfers:
            if (
                transfer["from"] == wallet_lower
                and transfer["to"] in BURN_ADDRESSES
            ):
                return "（买入后已销毁）"

    if trade_type == "SELL":
        for transfer in transfers:
            if (
                transfer["from"] == ZERO_ADDRESS.lower()
                and transfer["to"] == wallet_lower
            ):
                return "（来自增发）"

    return ""


# =========================================================
# Swap查询
# =========================================================

def get_logs_with_split(
    pair_contract: Any,
    from_block: int,
    to_block: int,
) -> list[Any]:
    """
    查询Swap日志。
    如果RPC限制区块范围，自动拆成两半继续查询。
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
                f"区块{from_block}查询失败：{error}"
            ) from error

        middle = (from_block + to_block) // 2

        print(
            f"查询范围受限，自动拆分："
            f"{from_block}-{middle}、"
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


# =========================================================
# 处理Swap
# =========================================================

def process_swap(
    web3: Web3,
    event: Any,
    ibs_is_token0: bool,
    ibs_decimals: int,
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
            f"忽略：{trade_type} "
            f"{format_amount(ibs_amount)} IBS",
            flush=True,
        )
        return

    tx_hash = event["transactionHash"].hex()
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


# =========================================================
# 主程序
# =========================================================

def main() -> None:
    print("IBS监控程序启动", flush=True)

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
            "Pair中没有找到IBS合约地址，"
            "请检查PAIR_ADDRESS"
        )

    ibs_decimals = int(
        ibs_contract.functions.decimals().call()
    )

    try:
        symbol = ibs_contract.functions.symbol().call()
    except Exception:
        symbol = "IBS"

    print(
        f"代币：{symbol}，"
        f"decimals：{ibs_decimals}，"
        f"IBS为token{'0' if ibs_is_token0 else '1'}",
        flush=True,
    )

    latest_block = web3.eth.block_number
    safe_latest = latest_block - CONFIRMATION_BLOCKS

    last_block = read_last_block(safe_latest)
    start_block = last_block + 1

    if start_block > safe_latest:
        print("没有新区块需要扫描", flush=True)
        return

    print(
        f"扫描范围：{start_block}-{safe_latest}",
        flush=True,
    )

    current_block = start_block

    while current_block <= safe_latest:
        end_block = min(
            current_block + BLOCK_CHUNK_SIZE - 1,
            safe_latest,
        )

        print(
            f"正在扫描：{current_block}-{end_block}",
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
                )

            # 该范围全部成功后，才更新进度
            save_last_block(end_block)
            current_block = end_block + 1

        except Exception as error:
            print(
                f"扫描失败：{error}",
                flush=True,
            )

            print(
                "等待3秒并切换RPC重试……",
                flush=True,
            )

            time.sleep(3)
            web3 = connect_web3()

            pair_contract = web3.eth.contract(
                address=PAIR_ADDRESS,
                abi=PAIR_ABI,
            )

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
