import os
import requests
from web3 import Web3
from web3.middleware import geth_poa_middleware


BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]


RPC = "https://bsc-dataseed.bnbchain.org"


TOKEN = Web3.to_checksum_address(
    "0x255e746abb8d9acac00d6d023e5e63e3b8dfa7cd"
)


WALLET = Web3.to_checksum_address(
    "0xed8b85788e15305c59de904fcaac0f2c9c4bd41b"
)


ZERO = "0x0000000000000000000000000000000000000000"


w3 = Web3(
    Web3.HTTPProvider(
        RPC,
        request_kwargs={"timeout":30}
    )
)


w3.middleware_onion.inject(
    geth_poa_middleware,
    layer=0
)


if not w3.is_connected():
    raise Exception("BSC连接失败")


print("BSC Connected")


TRANSFER_TOPIC = Web3.keccak(
    text="Transfer(address,address,uint256)"
).hex()


def topic_address(addr):

    return Web3.to_hex(
        Web3.to_bytes(
            hexstr=addr
        ).rjust(32,b'\x00')
    )


wallet_topic = topic_address(WALLET)

zero_topic = topic_address(ZERO)



latest = w3.eth.block_number


# 回扫200区块
start = latest - 200


print(
    f"扫描 {start}-{latest}"
)


queries = [

    # 钱包收到
    {
        "fromBlock": start,
        "toBlock": latest,
        "address": TOKEN,
        "topics":[
            TRANSFER_TOPIC,
            None,
            wallet_topic
        ]
    },


    # 钱包转出
    {
        "fromBlock": start,
        "toBlock": latest,
        "address": TOKEN,
        "topics":[
            TRANSFER_TOPIC,
            wallet_topic,
            None
        ]
    },


    # Mint
    {
        "fromBlock": start,
        "toBlock": latest,
        "address": TOKEN,
        "topics":[
            TRANSFER_TOPIC,
            zero_topic,
            wallet_topic
        ]
    }

]


seen=set()


for q in queries:


    try:

        logs=w3.eth.get_logs(q)


    except Exception as e:

        print("RPC错误:",e)
        continue



    for log in logs:


        tx=log.transactionHash.hex()


        if tx in seen:
            continue

        seen.add(tx)



        amount=int(
            log.data.hex(),
            16
        ) / 10**18



        if amount < 100:
            continue



        frm=Web3.to_checksum_address(
            "0x"+log.topics[1].hex()[-40:]
        )

        to=Web3.to_checksum_address(
            "0x"+log.topics[2].hex()[-40:]
        )



        msg=f"""
🚨 IBS 动作

数量:
{amount:,.4f} IBS

From:
{frm}

To:
{to}

区块:
{log.blockNumber}

TX:
https://bscscan.com/tx/{tx}
"""

        print(msg)


        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id":CHAT_ID,
                "text":msg
            },
            timeout=20
        )


print("完成")
