import os
import requests
from web3 import Web3

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

RPC = "https://bsc-dataseed.binance.org"

TOKEN = Web3.to_checksum_address(
    "0x255e746abb8d9acac00d6d023e5e63e3b8dfa7cd"
)

WALLET = Web3.to_checksum_address(
    "0xed8b85788e15305c59de904fcaac0f2c9c4bd41b"
)

STATE = "last_block.txt"


w3 = Web3(
    Web3.HTTPProvider(
        RPC,
        request_kwargs={"timeout": 20}
    )
)

if not w3.is_connected():
    raise Exception("RPC连接失败")


latest = w3.eth.block_number


# 读取上次区块
if os.path.exists(STATE):
    with open(STATE) as f:
        last_block = int(f.read().strip())
else:
    last_block = latest - 2


if last_block >= latest:
    print("没有新区块")
    exit()


print(
    f"扫描区块 {last_block+1} - {latest}"
)


TRANSFER_TOPIC = Web3.keccak(
    text="Transfer(address,address,uint256)"
).hex()


wallet_topic = (
    "0x000000000000000000000000"
    + WALLET[2:].lower()
)


logs = []


# 监听：
# 1. 转入钱包
# 2. 钱包转出

filters = [

    [
        TRANSFER_TOPIC,
        None,
        wallet_topic
    ],

    [
        TRANSFER_TOPIC,
        wallet_topic,
        None
    ]

]


for topics in filters:

    try:

        result = w3.eth.get_logs(
            {
                "address": TOKEN,
                "fromBlock": last_block + 1,
                "toBlock": latest,
                "topics": topics
            }
        )

        logs.extend(result)

    except Exception as e:

        print(
            "RPC错误:",
            e
        )


ZERO = "0x0000000000000000000000000000000000000000"


for log in logs:

    tx_hash = log["transactionHash"].hex()

    block = log["blockNumber"]


    from_addr = Web3.to_checksum_address(
        "0x" + log["topics"][1].hex()[-40:]
    )

    to_addr = Web3.to_checksum_address(
        "0x" + log["topics"][2].hex()[-40:]
    )


    amount = (
        int(log["data"].hex(),16)
        /
        10**18
    )


    message = None


    # Mint
    if (
        from_addr == ZERO
        and
        to_addr == WALLET
    ):

        message = f"""
🚨 IBS 增发 Mint

数量:
{amount:,.6f} IBS

接收钱包:
{WALLET}

区块:
{block}

交易:
https://bscscan.com/tx/{tx_hash}
"""


    # 钱包收到
    elif to_addr == WALLET:

        message = f"""
🟢 IBS 转入

数量:
{amount:,.6f} IBS

来源:
{from_addr}

区块:
{block}

交易:
https://bscscan.com/tx/{tx_hash}
"""


    # 钱包转出
    elif from_addr == WALLET:

        message = f"""
🔴 IBS 转出

数量:
{amount:,.6f} IBS

目标:
{to_addr}

区块:
{block}

交易:
https://bscscan.com/tx/{tx_hash}
"""


    if message:

        print(message)


        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=20
        )


        print(
            "Telegram:",
            r.text
        )


# 保存最新区块

with open(STATE,"w") as f:
    f.write(str(latest))


print("完成")
