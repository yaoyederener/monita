import os
import requests
from web3 import Web3
from web3.middleware import geth_poa_middleware


BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]


RPC = "https://bsc-dataseed.binance.org"


TOKEN = Web3.to_checksum_address(
    "0x255e746abb8d9acac00d6d023e5e63e3b8dfa7cd"
)


WALLET = Web3.to_checksum_address(
    "0xed8b85788e15305c59de904fcaac0f2c9c4bd41b"
)


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


ZERO = "0x0000000000000000000000000000000000000000"


latest = w3.eth.block_number


# 每次回扫100个区块
start_block = latest - 100


print(
    f"扫描区块 {start_block}-{latest}"
)


sent = set()


for block_number in range(
    start_block,
    latest + 1
):

    try:

        logs = w3.eth.get_logs({

            "fromBlock": block_number,
            "toBlock": block_number,
            "address": TOKEN,
            "topics": [
                TRANSFER_TOPIC
            ]

        })


    except Exception as e:

        print(
            "RPC错误:",
            e
        )

        continue



    for log in logs:


        txhash = log.transactionHash.hex()


        if txhash in sent:
            continue


        sent.add(txhash)



        from_addr = Web3.to_checksum_address(
            "0x" + log.topics[1].hex()[-40:]
        )


        to_addr = Web3.to_checksum_address(
            "0x" + log.topics[2].hex()[-40:]
        )


        amount = (
            int(log.data.hex(),16)
            /
            10**18
        )


        msg = None



        # Mint

        if (
            from_addr.lower()
            ==
            ZERO.lower()
            and
            to_addr.lower()
            ==
            WALLET.lower()
        ):

            msg=f"""
🚨 IBS Mint

数量:
{amount:,.4f} IBS

接收:
{to_addr}

区块:
{block_number}

TX:
https://bscscan.com/tx/{txhash}
"""



        # 转入

        elif (
            to_addr.lower()
            ==
            WALLET.lower()
            and
            amount >= 100
        ):

            msg=f"""
🟢 IBS 收入

数量:
{amount:,.4f} IBS

来源:
{from_addr}

TX:
https://bscscan.com/tx/{txhash}
"""



        # 转出

        elif (
            from_addr.lower()
            ==
            WALLET.lower()
            and
            amount >= 100
        ):

            msg=f"""
🔴 IBS 转出

数量:
{amount:,.4f} IBS

目标:
{to_addr}

TX:
https://bscscan.com/tx/{txhash}
"""



        if msg:

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
