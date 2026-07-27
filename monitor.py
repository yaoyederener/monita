import os
import sys
import time
from decimal import Decimal

import requests
from web3 import Web3


# =========================
# 基本配置
# =========================

PAIR_ADDRESS = Web3.to_checksum_address(
    "0x2a4B99A9c4544D35e8D266111c50B67fEA01d53d"
)

IBS_ADDRESS = Web3.to_checksum_address(
    "0x255e746abb8d9acac00d6d023e5e63e3b8dfa7cd"
)

MIN_IBS_AMOUNT = Decimal("50")
LAST_BLOCK_FILE = "last_block.txt"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()

# 这些节点支持 eth_getLogs。
# 某个节点失败时，程序会自动尝试下一个。
RPC_LIST = [
    "https://bsc-rpc.publicnode.com",
    "https://bnb.rpc.subquery.network/public",
]

# 每次最多查询多少个区块，降低公共 RPC 压力
BLOCK_CHUNK_SIZE = 1000

# 第一次运行时，只从最近多少个区块开始
FIRST_RUN_BLOCKS = 500


# =========================
# 最小 ABI
# =========================

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


def connect_web3():
    """连接可用的免费 BSC RPC。"""

    for rpc_url in RPC_LIST:
        try:
            web3 = Web3(
                Web3.HTTPProvider(
                    rpc_url,
                    request_kwargs={"timeout": 30},
                )
            )

            if not web3.is_connected():
                print(f"RPC 无法连接：{rpc_url}")
                continue

            chain_id = web3.eth.chain_id
            if chain_id != 56:
                print(f"RPC Chain ID 错误：{rpc_url}，Chain ID={chain_id}")
                continue

            latest = web3.eth.block_number
            print(f"RPC 已连接：{rpc_url}")
            print(f"BSC 最新区块：{latest}")
            return web3

        except Exception as exc:
            print(f"RPC 连接失败：{rpc_url}")
            print(exc)

    raise RuntimeError("所有 BSC RPC 都无法连接")


def read_last_block(latest_block):
    """读取上次成功扫描到的区块。"""

    if not os.path.exists(LAST_BLOCK_FILE):
        first_block = max(1, latest_block - FIRST_RUN_BLOCKS)
        print(f"没有 last_block.txt，从区块 {first_block} 开始")
        return first_block - 1

    try:
        with open(LAST_BLOCK_FILE, "r", encoding="utf-8") as file:
            value = int(file.read().strip())

        if value <= 0 or value > latest_block:
            raise ValueError("区块号无效")

        return value

    except Exception as exc:
        print(f"读取 last_block.txt 失败：{exc}")
        first_block = max(1, latest_block - FIRST_RUN_BLOCKS)
        return first_block - 1


def save_last_block(block_number):
    """保存已经成功处理完的区块号。"""

    temporary_file = f"{LAST_BLOCK_FILE}.tmp"

    with open(temporary_file, "w", encoding="utf-8") as file:
        file.write(str(block_number))

    os.replace(temporary_file, LAST_BLOCK_FILE)
    print(f"已保存最新区块：{block_number}")


def short_address(address):
    address = str(address)
    return f"{address[:8]}...{address[-6:]}"


def format_amount(value):
    """删除小数末尾多余的 0。"""

    text = f"{value:.6f}"
    return text.rstrip("0").rstrip(".")


def send_telegram(message):
    """发送 Telegram 消息。"""

    if not BOT_TOKEN:
        raise RuntimeError("GitHub Secret BOT_TOKEN 没有设置")

    if not CHAT_ID:
        raise RuntimeError("GitHub Secret CHAT_ID 没有设置")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=30,
    )

    if not response.ok:
        raise RuntimeError(
            f"Telegram 发送失败：{response.status_code} {response.text}"
        )

    print("Telegram 消息发送成功")


def get_swap_logs(pair_contract, from_block, to_block):
    """读取指定区块范围内 Pair 的 Swap 事件。"""

    return pair_contract.events.Swap.get_logs(
        from_block=from_block,
        to_block=to_block,
    )


def process_swap(web3, event, ibs_is_token0, ibs_decimals):
    """判断 Swap 是买入还是卖出。"""

    args = event["args"]

    if ibs_is_token0:
        raw_ibs_in = int(args["amount0In"])
        raw_ibs_out = int(args["amount0Out"])
    else:
        raw_ibs_in = int(args["amount1In"])
        raw_ibs_out = int(args["amount1Out"])

    divisor = Decimal(10) ** ibs_decimals

    # IBS 从用户进入池子：卖出
    if raw_ibs_in > 0:
        trade_type = "SELL"
        ibs_amount = Decimal(raw_ibs_in) / divisor
        icon = "🔴"
        chinese_name = "IBS 大额卖出"

    # IBS 从池子流出：买入
    elif raw_ibs_out > 0:
        trade_type = "BUY"
        ibs_amount = Decimal(raw_ibs_out) / divisor
        icon = "🟢"
        chinese_name = "IBS 大额买入"

    else:
        return

    if ibs_amount < MIN_IBS_AMOUNT:
        print(
            f"忽略小额交易：{trade_type} "
            f"{format_amount(ibs_amount)} IBS"
        )
        return

    tx_hash = event["transactionHash"].hex()
    block_number = int(event["blockNumber"])

    try:
        transaction = web3.eth.get_transaction(tx_hash)
        wallet = transaction["from"]
    except Exception:
        wallet = args.get("to", "未知地址")

    message = (
        f"{icon} {chinese_name}\n\n"
        f"数量：{format_amount(ibs_amount)} IBS\n"
        f"钱包：{short_address(wallet)}\n"
        f"区块：{block_number}\n\n"
        f"https://bscscan.com/tx/{tx_hash}"
    )

    print("=" * 60)
    print(message)
    send_telegram(message)


def main():
    web3 = connect_web3()

    pair_contract = web3.eth.contract(
        address=PAIR_ADDRESS,
        abi=PAIR_ABI,
    )

    ibs_contract = web3.eth.contract(
        address=IBS_ADDRESS,
        abi=TOKEN_ABI,
    )

    token0 = Web3.to_checksum_address(
        pair_contract.functions.token0().call()
    )
    token1 = Web3.to_checksum_address(
        pair_contract.functions.token1().call()
    )

    print(f"Pair token0：{token0}")
    print(f"Pair token1：{token1}")

    if IBS_ADDRESS == token0:
        ibs_is_token0 = True
    elif IBS_ADDRESS == token1:
        ibs_is_token0 = False
    else:
        raise RuntimeError("这个 Pair 合约中没有找到 IBS 代币")

    ibs_decimals = int(ibs_contract.functions.decimals().call())

    try:
        ibs_symbol = ibs_contract.functions.symbol().call()
    except Exception:
        ibs_symbol = "IBS"

    print(f"代币：{ibs_symbol}")
    print(f"Decimals：{ibs_decimals}")
    print(f"IBS 是 token{'0' if ibs_is_token0 else '1'}")

    latest_block = web3.eth.block_number

    # 留出几个区块，降低区块重组造成的影响
    safe_latest_block = max(1, latest_block - 3)

    last_block = read_last_block(safe_latest_block)
    start_block = last_block + 1

    if start_block > safe_latest_block:
        print("目前没有新区块需要扫描")
        return

    print(
        f"准备扫描：{start_block} "
        f"到 {safe_latest_block}"
    )

    current_block = start_block

    while current_block <= safe_latest_block:
        end_block = min(
            current_block + BLOCK_CHUNK_SIZE - 1,
            safe_latest_block,
        )

        try:
            print(f"扫描区块：{current_block} - {end_block}")

            events = get_swap_logs(
                pair_contract,
                current_block,
                end_block,
            )

            print(f"发现 Swap：{len(events)}")

            for event in events:
                process_swap(
                    web3=web3,
                    event=event,
                    ibs_is_token0=ibs_is_token0,
                    ibs_decimals=ibs_decimals,
                )

            # 这一段全部处理成功后再保存进度
            save_last_block(end_block)
            current_block = end_block + 1

        except Exception as exc:
            print(f"扫描失败：{exc}")

            # 短暂等待后重连其他 RPC，再重试当前区块段
            time.sleep(3)
            web3 = connect_web3()

            pair_contract = web3.eth.contract(
                address=PAIR_ADDRESS,
                abi=PAIR_ABI,
            )

    print("本次监控完成")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"程序运行失败：{error}")
        sys.exit(1)
