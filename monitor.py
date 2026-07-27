import os
import sys
import time
from pathlib import Path
from collections import defaultdict
from decimal import Decimal, getcontext
from typing import Any

import requests
from web3 import Web3


# ============================================================
# 你要监控的固定信息
# ============================================================

# 被监控的钱包地址
WATCH_ADDRESS = "0xed8b85788e15305c59de904fcaac0f2c9c4bd41b"

# 被监控的 BSC 代币合约地址
TOKEN_ADDRESS = "0x255e746abb8d9acac00d6d023e5e63e3b8dfa7cd"


# ============================================================
# GitHub Secrets
# 名称按照你现在已经配置好的名称
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# 保留你原来配置的名称，但本程序不依赖 BscScan API
BSCSCAN_KEY = os.getenv("BSCSCAN_KEY", "").strip()


# ============================================================
# BSC RPC
# ============================================================

BSC_RPC_URL = os.getenv(
    "BSC_RPC_URL",
    "https://bsc-dataseed.binance.org/"
).strip()

# 第一次运行检查最近多少个区块
FIRST_RUN_LOOKBACK = int(os.getenv("FIRST_RUN_LOOKBACK", "200"))

# 每次最多扫描多少个区块
MAX_BLOCKS_PER_RUN = int(os.getenv("MAX_BLOCKS_PER_RUN", "3000"))

# 每次 RPC 查询最多扫描多少个区块
LOG_BATCH_SIZE = int(os.getenv("LOG_BATCH_SIZE", "500"))

# 保存上一次扫描的区块
STATE_FILE = Path("last_block.txt")

# 提高 Decimal 精度
getcontext().prec = 50


# ERC-20 Transfer(address,address,uint256)
TRANSFER_TOPIC = Web3.keccak(
    text="Transfer(address,address,uint256)"
).hex()


def validate_config() -> None:
    """检查必要设置。"""

    missing = []

    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")

    if not CHAT_ID:
        missing.append("CHAT_ID")

    if missing:
        print("缺少 GitHub Secrets：")

        for item in missing:
            print(f"- {item}")

        print(
            "\n请进入 GitHub："
            "Settings → Secrets and variables → Actions"
        )
        sys.exit(1)

    if not Web3.is_address(WATCH_ADDRESS):
        print("WATCH_ADDRESS 地址格式错误：", WATCH_ADDRESS)
        sys.exit(1)

    if not Web3.is_address(TOKEN_ADDRESS):
        print("TOKEN_ADDRESS 地址格式错误：", TOKEN_ADDRESS)
        sys.exit(1)


def connect_bsc() -> Web3:
    """连接 BSC 主网。"""

    web3 = Web3(
        Web3.HTTPProvider(
            BSC_RPC_URL,
            request_kwargs={"timeout": 40},
        )
    )

    if not web3.is_connected():
        print("BSC RPC 连接失败：", BSC_RPC_URL)
        sys.exit(1)

    chain_id = web3.eth.chain_id

    if chain_id != 56:
        print("当前 RPC 不是 BSC 主网，Chain ID：", chain_id)
        sys.exit(1)

    print("BSC RPC 连接成功")
    print("Chain ID：", chain_id)

    return web3


def send_telegram(message: str) -> bool:
    """向 Telegram 群发送消息。"""

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=30,
        )

        if response.status_code != 200:
            print("Telegram 发送失败：", response.status_code)
            print(response.text)
            return False

        result = response.json()

        if not result.get("ok"):
            print("Telegram API 返回错误：", result)
            return False

        print("Telegram 通知发送成功")
        return True

    except requests.RequestException as exc:
        print("Telegram 网络错误：", exc)
        return False


def read_last_block() -> int | None:
    """读取上次扫描的区块。"""

    if not STATE_FILE.exists():
        return None

    try:
        text = STATE_FILE.read_text(
            encoding="utf-8"
        ).strip()

        if not text:
            return None

        return int(text)

    except (OSError, ValueError) as exc:
        print("读取 last_block.txt 失败：", exc)
        return None


def save_last_block(block_number: int) -> None:
    """保存本次完成扫描的区块。"""

    STATE_FILE.write_text(
        str(block_number),
        encoding="utf-8",
    )

    print("已保存最后扫描区块：", block_number)


def topic_to_address(topic: Any) -> str:
    """将日志中的 indexed address 转换成普通地址。"""

    topic_hex = Web3.to_hex(topic)

    # topic 是 32 字节，地址取最后 20 字节
    address = "0x" + topic_hex[-40:]

    return Web3.to_checksum_address(address)


def data_to_int(data: Any) -> int:
    """将日志 data 转换成整数。"""

    if isinstance(data, bytes):
        return int.from_bytes(data, byteorder="big")

    data_hex = Web3.to_hex(data)

    return int(data_hex, 16)


def safe_contract_call(
    contract_function,
    default_value
):
    """安全调用代币合约方法。"""

    try:
        return contract_function.call()
    except Exception as exc:
        print("读取代币信息失败：", exc)
        return default_value


def get_token_information(web3: Web3) -> tuple[str, int]:
    """读取代币 symbol 和 decimals。"""

    token_abi = [
        {
            "constant": True,
            "inputs": [],
            "name": "symbol",
            "outputs": [
                {
                    "name": "",
                    "type": "string",
                }
            ],
            "type": "function",
        },
        {
            "constant": True,
            "inputs": [],
            "name": "decimals",
            "outputs": [
                {
                    "name": "",
                    "type": "uint8",
                }
            ],
            "type": "function",
        },
    ]

    contract = web3.eth.contract(
        address=Web3.to_checksum_address(TOKEN_ADDRESS),
        abi=token_abi,
    )

    symbol = safe_contract_call(
        contract.functions.symbol(),
        "TOKEN",
    )

    decimals = safe_contract_call(
        contract.functions.decimals(),
        18,
    )

    return str(symbol), int(decimals)


def format_token_amount(
    raw_amount: int,
    decimals: int,
) -> str:
    """格式化代币数量。"""

    divisor = Decimal(10) ** decimals
    amount = Decimal(raw_amount) / divisor

    formatted = f"{amount:f}"

    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")

    return formatted or "0"


def format_bnb_amount(raw_wei: int) -> str:
    """格式化 BNB 数量。"""

    amount = Decimal(raw_wei) / Decimal(10**18)
    formatted = f"{amount:f}"

    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")

    return formatted or "0"


def short_address(address: str) -> str:
    """缩短地址用于日志显示。"""

    if len(address) < 16:
        return address

    return f"{address[:8]}...{address[-6:]}"


def get_transfer_logs(
    web3: Web3,
    from_block: int,
    to_block: int,
) -> list:
    """分批读取指定代币的 Transfer 日志。"""

    all_logs = []

    current_start = from_block

    while current_start <= to_block:
        current_end = min(
            current_start + LOG_BATCH_SIZE - 1,
            to_block,
        )

        print(
            f"查询代币日志："
            f"{current_start} → {current_end}"
        )

        try:
            logs = web3.eth.get_logs(
                {
                    "fromBlock": current_start,
                    "toBlock": current_end,
                    "address": Web3.to_checksum_address(
                        TOKEN_ADDRESS
                    ),
                    "topics": [TRANSFER_TOPIC],
                }
            )

            all_logs.extend(logs)

        except Exception as exc:
            print(
                f"读取区块日志失败 "
                f"{current_start} → {current_end}：{exc}"
            )

            # 公共 RPC 失败时稍等再重试一次
            time.sleep(3)

            try:
                logs = web3.eth.get_logs(
                    {
                        "fromBlock": current_start,
                        "toBlock": current_end,
                        "address": Web3.to_checksum_address(
                            TOKEN_ADDRESS
                        ),
                        "topics": [TRANSFER_TOPIC],
                    }
                )

                all_logs.extend(logs)

            except Exception as retry_exc:
                print("重试仍然失败：", retry_exc)
                raise

        current_start = current_end + 1

    return all_logs


def analyse_logs(
    logs: list,
    watched_address: str,
) -> dict[str, dict]:
    """
    按交易哈希汇总指定钱包的代币净变化。

    收到代币：正数
    发出代币：负数
    """

    transactions = defaultdict(
        lambda: {
            "net_amount": 0,
            "received": 0,
            "sent": 0,
            "block_number": 0,
            "log_count": 0,
            "counterparties": set(),
        }
    )

    watched_lower = watched_address.lower()

    for log in logs:
        if len(log["topics"]) < 3:
            continue

        sender = topic_to_address(log["topics"][1])
        receiver = topic_to_address(log["topics"][2])
        amount = data_to_int(log["data"])

        sender_lower = sender.lower()
        receiver_lower = receiver.lower()

        if (
            sender_lower != watched_lower
            and receiver_lower != watched_lower
        ):
            continue

        tx_hash = log["transactionHash"].hex()
        item = transactions[tx_hash]

        item["block_number"] = int(log["blockNumber"])
        item["log_count"] += 1

        if receiver_lower == watched_lower:
            item["net_amount"] += amount
            item["received"] += amount

            if sender_lower != watched_lower:
                item["counterparties"].add(sender)

        if sender_lower == watched_lower:
            item["net_amount"] -= amount
            item["sent"] += amount

            if receiver_lower != watched_lower:
                item["counterparties"].add(receiver)

    return dict(transactions)


def determine_transaction_type(net_amount: int) -> tuple[str, str]:
    """
    根据代币净变化判断方向。

    注意：普通转账也可能被判断为买入或卖出，
    因此通知中使用“疑似”。
    """

    if net_amount > 0:
        return "🟢 疑似买入", "买入/收到"

    if net_amount < 0:
        return "🔴 疑似卖出", "卖出/转出"

    return "🟡 代币交互", "净变化为零"


def send_transaction_notification(
    web3: Web3,
    tx_hash: str,
    information: dict,
    token_symbol: str,
    token_decimals: int,
) -> None:
    """读取交易详情并发送通知。"""

    try:
        transaction = web3.eth.get_transaction(tx_hash)
        receipt = web3.eth.get_transaction_receipt(tx_hash)
        block = web3.eth.get_block(
            information["block_number"]
        )

    except Exception as exc:
        print("读取交易详情失败：", tx_hash, exc)
        return

    net_amount = information["net_amount"]
    absolute_amount = abs(net_amount)

    title, direction = determine_transaction_type(net_amount)

    token_amount = format_token_amount(
        absolute_amount,
        token_decimals,
    )

    received_amount = format_token_amount(
        information["received"],
        token_decimals,
    )

    sent_amount = format_token_amount(
        information["sent"],
        token_decimals,
    )

    bnb_value = format_bnb_amount(
        int(transaction.get("value", 0))
    )

    status = (
        "成功"
        if int(receipt["status"]) == 1
        else "失败"
    )

    timestamp = int(block["timestamp"])

    local_time = time.strftime(
        "%Y-%m-%d %H:%M:%S UTC",
        time.gmtime(timestamp),
    )

    counterparties = list(
        information["counterparties"]
    )

    if counterparties:
        counterpart_text = "\n".join(
            f"<code>{address}</code>"
            for address in counterparties[:5]
        )
    else:
        counterpart_text = "未知"

    sender = transaction.get("from") or "未知"
    receiver = transaction.get("to") or "合约创建"

    bscscan_url = (
        f"https://bscscan.com/tx/{tx_hash}"
    )

    message = (
        f"<b>{title}</b>\n\n"
        f"<b>方向：</b>{direction}\n"
        f"<b>代币：</b>{token_symbol}\n"
        f"<b>本次净变化：</b>{token_amount} {token_symbol}\n"
        f"<b>收到数量：</b>{received_amount} {token_symbol}\n"
        f"<b>发出数量：</b>{sent_amount} {token_symbol}\n"
        f"<b>附带 BNB：</b>{bnb_value} BNB\n"
        f"<b>交易状态：</b>{status}\n"
        f"<b>区块：</b>{information['block_number']}\n"
        f"<b>时间：</b>{local_time}\n\n"
        f"<b>监控钱包：</b>\n"
        f"<code>{WATCH_ADDRESS}</code>\n\n"
        f"<b>交易发送方：</b>\n"
        f"<code>{sender}</code>\n\n"
        f"<b>交易接收方：</b>\n"
        f"<code>{receiver}</code>\n\n"
        f"<b>相关地址：</b>\n"
        f"{counterpart_text}\n\n"
        f"<b>代币合约：</b>\n"
        f"<code>{TOKEN_ADDRESS}</code>\n\n"
        f"<b>交易哈希：</b>\n"
        f"<code>{tx_hash}</code>\n\n"
        f'<a href="{bscscan_url}">'
        f"点击查看 BscScan 交易详情"
        f"</a>"
    )

    print("=" * 60)
    print(title)
    print("交易哈希：", tx_hash)
    print("数量：", token_amount, token_symbol)
    print("区块：", information["block_number"])
    print("状态：", status)

    send_telegram(message)


def main() -> None:
    """程序主入口。"""

    print("=" * 60)
    print("BSC Token Buy/Sell Monitor")
    print("=" * 60)

    validate_config()

    web3 = connect_bsc()

    watched_address = Web3.to_checksum_address(
        WATCH_ADDRESS
    )

    token_address = Web3.to_checksum_address(
        TOKEN_ADDRESS
    )

    print("监控钱包：", watched_address)
    print("代币合约：", token_address)

    token_symbol, token_decimals = get_token_information(
        web3
    )

    print("代币名称：", token_symbol)
    print("代币精度：", token_decimals)

    latest_block = web3.eth.block_number
    last_block = read_last_block()

    print("BSC 当前区块：", latest_block)
    print("上次扫描区块：", last_block)

    if last_block is None:
        start_block = max(
            0,
            latest_block - FIRST_RUN_LOOKBACK + 1,
        )

        print(
            "第一次运行，扫描最近 "
            f"{FIRST_RUN_LOOKBACK} 个区块"
        )
    else:
        start_block = last_block + 1

    if start_block > latest_block:
        print("当前没有新区块")
        save_last_block(latest_block)
        print("完成")
        return

    total_blocks = latest_block - start_block + 1

    if total_blocks > MAX_BLOCKS_PER_RUN:
        start_block = (
            latest_block - MAX_BLOCKS_PER_RUN + 1
        )

        print(
            "未扫描区块过多，本次只扫描最近 "
            f"{MAX_BLOCKS_PER_RUN} 个区块"
        )

    print(
        f"本次扫描范围："
        f"{start_block} → {latest_block}"
    )

    try:
        logs = get_transfer_logs(
            web3,
            start_block,
            latest_block,
        )

    except Exception as exc:
        print("获取日志失败，本次不更新区块记录：", exc)
        sys.exit(1)

    print("共读取 Transfer 日志：", len(logs))

    transactions = analyse_logs(
        logs,
        watched_address,
    )

    print("发现相关交易：", len(transactions))

    sorted_transactions = sorted(
        transactions.items(),
        key=lambda item: item[1]["block_number"],
    )

    for tx_hash, information in sorted_transactions:
        send_transaction_notification(
            web3=web3,
            tx_hash=tx_hash,
            information=information,
            token_symbol=token_symbol,
            token_decimals=token_decimals,
        )

    save_last_block(latest_block)

    print("=" * 60)
    print("相关交易数量：", len(transactions))
    print("扫描完成")


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print("\n程序已停止")
        sys.exit(0)

    except Exception as exc:
        print("程序发生错误：", repr(exc))

        if BOT_TOKEN and CHAT_ID:
            send_telegram(
                "<b>⚠️ BSC 监控程序运行失败</b>\n\n"
                f"<code>{str(exc)}</code>"
            )

        sys.exit(1)
