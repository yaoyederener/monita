import html
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from pathlib import Path

import requests
from web3 import Web3

# ===== Monitoring target =====
WALLET_ADDRESS = "0xCBA922f6aff0EC8CB0703D44249456Ef779A394C"

# Binance-Peg BSC-USD / commonly displayed as USDT on BNB Smart Chain
USDT_CONTRACT_ADDRESS = "0x55d398326f99059fF775485246999027B3197955"

STATE_FILE = Path("data/usdt_balance.json")
RPC_TIMEOUT_SECONDS = 30
TELEGRAM_TIMEOUT_SECONDS = 20

# Minimal BEP-20 ABI: only the functions this monitor needs
TOKEN_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
]


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少 GitHub Secret：{name}")
    return value


def format_token_amount(raw_amount: int, decimals: int) -> str:
    """Convert an integer token balance to a non-scientific decimal string."""
    getcontext().prec = max(80, decimals + 40)
    amount = Decimal(raw_amount) / (Decimal(10) ** decimals)
    formatted = f"{amount:,.{decimals}f}".rstrip("0").rstrip(".")
    return formatted or "0"


def load_previous_balance() -> int | None:
    if not STATE_FILE.exists():
        return None

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return int(data["raw_balance"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"状态文件读取失败：{exc}") from exc


def save_balance(raw_balance: int, decimals: int, block_number: int) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "chain": "BNB Smart Chain",
        "wallet": WALLET_ADDRESS,
        "token_contract": USDT_CONTRACT_ADDRESS,
        "raw_balance": str(raw_balance),
        "decimals": decimals,
        "block_number": block_number,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    STATE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def send_telegram(bot_token: str, chat_id: str, message: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=TELEGRAM_TIMEOUT_SECONDS,
    )

    try:
        result = response.json()
    except ValueError:
        result = {"ok": False, "description": response.text[:300]}

    if not response.ok or not result.get("ok"):
        raise RuntimeError(
            f"Telegram 推送失败：HTTP {response.status_code}，"
            f"{result.get('description', '未知错误')}"
        )


def main() -> None:
    rpc_url = require_env("BSC_RPC")
    bot_token = require_env("BOT_TOKEN")
    chat_id = require_env("CHAT_ID")

    print("USDT余额监控启动")
    print(f"监控地址：{WALLET_ADDRESS}")

    web3 = Web3(
        Web3.HTTPProvider(
            rpc_url,
            request_kwargs={"timeout": RPC_TIMEOUT_SECONDS},
        )
    )

    if not web3.is_connected():
        raise RuntimeError("BSC RPC 连接失败，请检查 BSC_RPC")

    wallet = Web3.to_checksum_address(WALLET_ADDRESS)
    token_address = Web3.to_checksum_address(USDT_CONTRACT_ADDRESS)
    token = web3.eth.contract(address=token_address, abi=TOKEN_ABI)

    block_number = web3.eth.block_number
    decimals = int(token.functions.decimals().call())
    current_raw = int(token.functions.balanceOf(wallet).call())
    previous_raw = load_previous_balance()

    current_display = format_token_amount(current_raw, decimals)
    print(f"当前区块：{block_number}")
    print(f"当前余额：{current_display} USDT")

    escaped_wallet = html.escape(WALLET_ADDRESS)
    address_url = f"https://bscscan.com/address/{WALLET_ADDRESS}"
    token_url = (
        f"https://bscscan.com/token/{USDT_CONTRACT_ADDRESS}"
        f"?a={WALLET_ADDRESS}"
    )

    if previous_raw is None:
        message = (
            "✅ <b>BSC USDT余额监控已启动</b>\n\n"
            f"地址：<code>{escaped_wallet}</code>\n"
            f"当前余额：<b>{current_display} USDT</b>\n"
            f"区块：<code>{block_number}</code>\n\n"
            f'<a href="{address_url}">查看地址</a> ｜ '
            f'<a href="{token_url}">查看USDT余额</a>'
        )
        send_telegram(bot_token, chat_id, message)
        save_balance(current_raw, decimals, block_number)
        print("首次运行：已发送基准余额并保存状态")
        return

    if current_raw == previous_raw:
        print("余额没有变化，不发送Telegram通知")
        return

    delta_raw = current_raw - previous_raw
    previous_display = format_token_amount(previous_raw, decimals)
    delta_display = format_token_amount(abs(delta_raw), decimals)

    if delta_raw > 0:
        direction = "📈 增加"
        sign = "+"
    else:
        direction = "📉 减少"
        sign = "-"

    message = (
        "🚨 <b>BSC USDT余额发生变化</b>\n\n"
        f"地址：<code>{escaped_wallet}</code>\n"
        f"变化：<b>{direction} {sign}{delta_display} USDT</b>\n"
        f"原余额：<code>{previous_display} USDT</code>\n"
        f"现余额：<b>{current_display} USDT</b>\n"
        f"区块：<code>{block_number}</code>\n\n"
        f'<a href="{address_url}">查看地址</a> ｜ '
        f'<a href="{token_url}">查看USDT记录</a>'
    )

    # Only write the new state after Telegram confirms the message was sent.
    # This lets the next run retry the alert if Telegram temporarily fails.
    send_telegram(bot_token, chat_id, message)
    save_balance(current_raw, decimals, block_number)
    print(f"余额变化：{sign}{delta_display} USDT")
    print("Telegram通知发送成功，状态已保存")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        raise
