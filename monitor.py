import os
import sys
from pathlib import Path
from typing import Optional

import requests
from web3 import Web3


# =========================================================
# 环境变量
# =========================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WATCH_ADDRESS = os.getenv("WATCH_ADDRESS", "").strip()

# 默认使用 Binance 官方公共 BSC RPC
BSC_RPC_URL = os.getenv(
    "BSC_RPC_URL",
    "https://bsc-dataseed.binance.org/"
).strip()

# 第一次运行时检查最近多少个区块
FIRST_RUN_LOOKBACK = int(os.getenv("FIRST_RUN_LOOKBACK", "20"))

# 每次最多扫描多少个区块，避免程序一次扫描过多
MAX_BLOCKS_PER_RUN = int(os.getenv("MAX_BLOCKS_PER_RUN", "100"))

# 保存上次扫描区块
STATE_FILE = Path("last_block.txt")

# PancakeSwap / 常见 DEX 方法选择器
SWAP_METHODS = {
    "0x38ed1739": "swapExactTokensForTokens",
    "0x8803dbee": "swapTokensForExactTokens",
    "0x7ff36ab5": "swapExactETHForTokens",
    "0x4a25d94a": "swapTokensForExactETH",
    "0x18cbafe5": "swapExactTokensForETH",
    "0xfb3bdb41": "swapETHForExactTokens",
    "0x5c11d795": "swapExactTokensForTokensSupportingFeeOnTransferTokens",
    "0xb6f9de95": "swapExactETHForTokensSupportingFeeOnTransferTokens",
    "0x791ac947": "swapExactTokensForETHSupportingFeeOnTransferTokens",

    # PancakeSwap Universal Router / Uniswap Universal Router
    "0x3593564c": "Universal Router execute",
    "0x24856bc3": "Universal Router execute",

    # 1inch
    "0x12aa3caf": "1inch swap",
    "0x0502b1c5": "1inch unoswap",

    # OpenOcean / Aggregator 常见调用
    "0x90411a32": "Aggregator swap",
}


def validate_config() -> None:
    """检查必要的环境变量。"""

    missing = []

    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")

    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if not WATCH_ADDRESS:
        missing.append("WATCH_ADDRESS")

    if missing:
        print("缺少环境变量：")
        for item in missing:
            print(f"- {item}")

        print("\n请在 GitHub 仓库 Settings → Secrets and variables → Actions 中添加。")
        sys.exit(1)

    if not Web3.is_address(WATCH_ADDRESS):
        print(f"WATCH_ADDRESS 不是有效的 BSC 地址：{WATCH_ADDRESS}")
        sys.exit(1)


def send_telegram(message: str) -> bool:
    """发送 Telegram 消息。"""

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=20,
        )

        if response.status_code != 200:
            print("Telegram发送失败：", response.status_code)
            print(response.text)
            return False

        result = response.json()

        if not result.get("ok"):
            print("Telegram返回错误：", result)
            return False

        return True

    except requests.RequestException as exc:
        print("Telegram网络错误：", exc)
        return False


def connect_bsc() -> Web3:
    """连接 BSC 节点。"""

    web3 = Web3(
        Web3.HTTPProvider(
            BSC_RPC_URL,
            request_kwargs={"timeout": 30},
        )
    )

    if not web3.is_connected():
        print(f"BSC RPC连接失败：{BSC_RPC_URL}")
        sys.exit(1)

    print("BSC Connected")
    print("Chain ID:", web3.eth.chain_id)

    return web3


def read_last_block() -> Optional[int]:
    """读取上一次扫描到的区块。"""

    if not STATE_FILE.exists():
        return None

    try:
        content = STATE_FILE.read_text(encoding="utf-8").strip()

        if not content:
            return None

        return int(content)

    except (ValueError, OSError) as exc:
        print("读取 last_block.txt 失败：", exc)
        return None


def save_last_block(block_number: int) -> None:
    """保存最新扫描区块。"""

    STATE_FILE.write_text(
        str(block_number),
        encoding="utf-8",
    )

    print("已保存区块：", block_number)


def normalize_input(transaction_input) -> str:
    """把交易 input 转换为十六进制字符串。"""

    if transaction_input is None:
        return "0x"

    if isinstance(transaction_input, str):
        return transaction_input.lower()

    try:
        return Web3.to_hex(transaction_input).lower()
    except Exception:
        return str(transaction_input).lower()


def identify_swap(transaction_input: str) -> Optional[str]:
    """根据交易方法选择器判断是否为常见 Swap。"""

    if not transaction_input or transaction_input == "0x":
        return None

    if len(transaction_input) < 10:
        return None

    method_id = transaction_input[:10].lower()
    return SWAP_METHODS.get(method_id)


def short_address(address: Optional[str]) -> str:
    """缩短地址显示。"""

    if not address:
        return "合约创建"

    if len(address) <= 14:
        return address

    return f"{address[:8]}...{address[-6:]}"


def format_bnb(web3: Web3, value: int) -> str:
    """Wei 转换为 BNB。"""

    try:
        amount = web3.from_wei(value, "ether")
        return f"{amount:.8f}".rstrip("0").rstrip(".")
    except Exception:
        return "0"


def check_transaction(
    web3: Web3,
    transaction,
    watched_address: str,
) -> bool:
    """
    检查单笔交易。

    返回 True 表示发现监控地址发起的交易。
    """

    sender = transaction.get("from")
    receiver = transaction.get("to")

    if not sender:
        return False

    if sender.lower() != watched_address.lower():
        return False

    tx_hash = transaction["hash"].hex()
    transaction_input = normalize_input(transaction.get("input"))
    method_id = (
        transaction_input[:10]
        if len(transaction_input) >= 10
        else "无"
    )

    swap_name = identify_swap(transaction_input)
    bnb_value = format_bnb(web3, transaction.get("value", 0))

    try:
        receipt = web3.eth.get_transaction_receipt(tx_hash)
        status = "成功" if receipt.status == 1 else "失败"
        gas_used = receipt.gasUsed
    except Exception as exc:
        print(f"读取交易回执失败 {tx_hash}：{exc}")
        status = "未知"
        gas_used = "未知"

    if swap_name:
        transaction_type = f"🔄 发现 Swap\n方法：{swap_name}"
    elif transaction_input not in ("", "0x"):
        transaction_type = (
            "📝 发现合约交易\n"
            f"方法ID：{method_id}"
        )
    else:
        transaction_type = "💸 发现普通 BNB 转账"

    explorer_url = f"https://bscscan.com/tx/{tx_hash}"

    message = (
        f"<b>🚨 BSC 地址监控通知</b>\n\n"
        f"{transaction_type}\n\n"
        f"<b>状态：</b>{status}\n"
        f"<b>区块：</b>{transaction['blockNumber']}\n"
        f"<b>发送方：</b><code>{sender}</code>\n"
        f"<b>接收方：</b><code>{receiver or '合约创建'}</code>\n"
        f"<b>BNB数量：</b>{bnb_value} BNB\n"
        f"<b>Gas Used：</b>{gas_used}\n"
        f"<b>交易哈希：</b><code>{tx_hash}</code>\n\n"
        f"<a href=\"{explorer_url}\">在 BscScan 查看交易</a>"
    )

    print("=" * 60)
    print(transaction_type)
    print("交易：", tx_hash)
    print("接收：", short_address(receiver))
    print("状态：", status)

    send_telegram(message)
    return True


def main() -> None:
    """程序入口。"""

    print("=" * 60)
    print("BSC Wallet Monitor")
    print("=" * 60)

    validate_config()

    watched_address = Web3.to_checksum_address(WATCH_ADDRESS)
    web3 = connect_bsc()

    latest_block = web3.eth.block_number
    previous_block = read_last_block()

    print("监控地址：", watched_address)
    print("当前区块：", latest_block)
    print("上次区块：", previous_block)

    if previous_block is None:
        start_block = max(
            0,
            latest_block - FIRST_RUN_LOOKBACK + 1,
        )

        print(
            f"第一次运行，检查最近 "
            f"{FIRST_RUN_LOOKBACK} 个区块"
        )
    else:
        start_block = previous_block + 1

    if start_block > latest_block:
        print("没有新区块。")
        save_last_block(latest_block)
        print("完成")
        return

    total_blocks = latest_block - start_block + 1

    if total_blocks > MAX_BLOCKS_PER_RUN:
        start_block = latest_block - MAX_BLOCKS_PER_RUN + 1
        total_blocks = MAX_BLOCKS_PER_RUN

        print(
            f"待扫描区块过多，只扫描最近 "
            f"{MAX_BLOCKS_PER_RUN} 个区块"
        )

    print(f"扫描范围：{start_block} → {latest_block}")
    print(f"总区块数：{total_blocks}")

    transaction_count = 0
    swap_count = 0

    for block_number in range(start_block, latest_block + 1):
        print(f"正在扫描区块：{block_number}")

        try:
            block = web3.eth.get_block(
                block_number,
                full_transactions=True,
            )
        except Exception as exc:
            print(f"读取区块 {block_number} 失败：{exc}")
            continue

        for transaction in block.transactions:
            sender = transaction.get("from")

            if not sender:
                continue

            if sender.lower() != watched_address.lower():
                continue

            transaction_input = normalize_input(
                transaction.get("input")
            )

            if identify_swap(transaction_input):
                swap_count += 1

            if check_transaction(
                web3,
                transaction,
                watched_address,
            ):
                transaction_count += 1

    save_last_block(latest_block)

    print("=" * 60)
    print("发现地址发起的交易：", transaction_count)
    print("其中识别为 Swap：", swap_count)
    print("完成")


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print("\n程序已停止。")

    except Exception as exc:
        print("程序发生未处理错误：", repr(exc))

        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            send_telegram(
                "<b>⚠️ BSC监控程序发生错误</b>\n\n"
                f"<code>{str(exc)}</code>"
            )

        sys.exit(1)
