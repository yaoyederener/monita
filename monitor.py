import os
import requests
from web3 import Web3

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

RPCS = [
    "https://bsc-dataseed.binance.org",
    "https://bsc-dataseed1.defibit.io",
    "https://bsc-dataseed1.ninicoin.io",
    "https://rpc.ankr.com/bsc",
]

TOKEN = Web3.to_checksum_address(
    "0x255e746abb8d9acac00d6d023e5e63e3b8dfa7cd"
)

WALLET = Web3.to_checksum_address(
    "0xed8b85788e15305c59de904fcaac0f2c9c4bd41b"
)

STATE = "last_balance.txt"


# 连接 BSC
w3 = None

for rpc in RPCS:
    try:
        temp = Web3(
            Web3.HTTPProvider(
                rpc,
                request_kwargs={"timeout": 20}
            )
        )

        if temp.is_connected():
            w3 = temp
            print("Connected:", rpc)
            break

    except Exception as e:
        print("RPC error:", e)


if w3 is None:
    raise Exception("BSC RPC连接失败")


ABI = [
    {
        "inputs": [
            {
                "name": "account",
                "type": "address"
            }
        ],
        "name": "balanceOf",
        "outputs": [
            {
                "type": "uint256"
            }
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [
            {
                "type": "uint8"
            }
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [
            {
                "type": "string"
            }
        ],
        "stateMutability": "view",
        "type": "function"
    }
]


contract = w3.eth.contract(
    address=TOKEN,
    abi=ABI
)


symbol = contract.functions.symbol().call()
decimals = contract.functions.decimals().call()

balance = contract.functions.balanceOf(
    WALLET
).call()


old_balance = None

if os.path.exists(STATE):
    with open(STATE, "r") as f:
        data = f.read().strip()

        if data:
            old_balance = int(data)


# 第一次运行
if old_balance is None:

    with open(STATE, "w") as f:
        f.write(str(balance))

    print("首次记录余额")
    exit()


# 余额变化
if balance != old_balance:

    old_amount = old_balance / (10 ** decimals)
    new_amount = balance / (10 ** decimals)

    change = new_amount - old_amount


    message = f"""
🚨 Token余额变化

Token:
{symbol}

变化:
{change:+,.6f}

当前余额:
{new_amount:,.6f}

钱包:
{WALLET}
"""


    print(message)


    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message
        },
        timeout=20
    )


    print("Telegram Response:")
    print(response.text)


    with open(STATE, "w") as f:
        f.write(str(balance))


else:

    print("余额未变化")
