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
        request_kwargs={
            "timeout":30
        }
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


latest = w3.eth.block_number


# 最近200区块
start_block = latest - 200


print(
    f"扫描区块 {start_block}-{latest}"
)



def addr_topic(address):

    return (
        "0x"
        +
        "0"*24
        +
        address.lower()[2:]
    )



wallet_topic = addr_topic(WALLET)



processed = set()



for start in range(
    start_block,
    latest + 1,
    5
):

    end = min(
        start + 4,
        latest
    )


    # 查钱包收到的币

    queries = [

        {
            "fromBlock": start,
            "toBlock": end,
            "address": TOKEN,
            "topics":[
                TRANSFER_TOPIC,
                None,
                wallet_topic
            ]
        },


        # 查钱包转出的币

        {
            "fromBlock": start,
            "toBlock": end,
            "address": TOKEN,
            "topics":[
                TRANSFER_TOPIC,
                wallet_topic,
                None
            ]
        },


        # 查Mint

        {
            "fromBlock": start,
            "toBlock": end,
            "address": TOKEN,
            "topics":[
                TRANSFER_TOPIC,
                addr_topic(ZERO),
                wallet_topic
            ]
        }

    ]



    for q in queries:


        try:

            logs = w3.eth.get_logs(q)


        except Exception as e:

            print(
                "RPC错误:",
                e
            )

            continue



        for log in logs:


            tx = log.transactionHash.hex()


            if tx in processed:
                continue


            processed.add(tx)



            from_addr = Web3.to_checksum_address(
                "0x" + log.topics[1].hex()[-40:]
            )


            to_addr = Web3.to_checksum_address(
                "0x" + log.topics[2].hex()[-40:]
            )


            amount = (

                int(
                    log.data.hex(),
                    16
                )

                /

                10**18

            )


            if amount < 100:
                continue



            msg = None



            if from_addr.lower() == ZERO.lower():


                msg=f"""
🚨 IBS 增发 Mint

数量:
{amount:,.4f} IBS

接收:
{to_addr}

区块:
{log.blockNumber}

TX:
https://bscscan.com/tx/{tx}
"""



            elif to_addr.lower() == WALLET.lower():


                msg=f"""
🟢 IBS 买入/收到

数量:
{amount:,.4f} IBS

来源:
{from_addr}

区块:
{log.blockNumber}

TX:
https://bscscan.com/tx/{tx}
"""



            elif from_addr.lower() == WALLET.lower():


                msg=f"""
🔴 IBS 卖出/转出

数量:
{amount:,.4f} IBS

目标:
{to_addr}

区块:
{log.blockNumber}

TX:
https://bscscan.com/tx/{tx}
"""



            if msg:


                print(msg)


                try:

                    requests.post(

                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",

                        json={

                            "chat_id":CHAT_ID,

                            "text":msg

                        },

                        timeout=20

                    )


                except Exception as e:

                    print(
                        "Telegram错误:",
                        e
                    )



print("完成")
