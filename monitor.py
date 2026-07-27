import os
import requests
from web3 import Web3
from web3.middleware import geth_poa_middleware


BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]


RPC = "https://bsc-dataseed.bnbchain.org"


PAIR = Web3.to_checksum_address(
    "0x2a4B99A9c4544D35e8D266111c50B67fEA01d53"
)


IBS = Web3.to_checksum_address(
    "0x255e746abb8d9acac00d6d023e5e63e3b8dfa7cd"
)


USDT = Web3.to_checksum_address(
    "0x55d398326f99059fF775485246999027B3197955"
)


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


# PancakeSwap V2 Swap事件

SWAP_TOPIC = Web3.keccak(
    text="Swap(address,uint256,uint256,uint256,uint256,address)"
).hex()



latest = w3.eth.block_number


# 最近100区块
start = latest - 100


print(
    f"扫描 {start}-{latest}"
)



try:

    logs = w3.eth.get_logs({

        "fromBlock": start,

        "toBlock": latest,

        "address": PAIR,

        "topics":[
            SWAP_TOPIC
        ]

    })


except Exception as e:

    print(
        "RPC错误:",
        e
    )

    exit()



for log in logs:


    data = log.data.hex()[2:]


    amount0In = int(
        data[0:64],
        16
    )


    amount1In = int(
        data[64:128],
        16
    )


    amount0Out = int(
        data[128:192],
        16
    )


    amount1Out = int(
        data[192:256],
        16
    )



    tx = log.transactionHash.hex()



    msg = None



    # 假设 token0 = IBS token1 = USDT

    # IBS卖出
    if amount0In > 0 and amount1Out > 0:

        ibs_amount = amount0In / 10**18

        usdt_amount = amount1Out / 10**18


        if ibs_amount >= 100:

            msg=f"""
🔴 IBS SELL

卖出:
{ibs_amount:,.4f} IBS

收到:
{usdt_amount:,.4f} USDT

区块:
{log.blockNumber}

TX:
https://bscscan.com/tx/{tx}
"""



    # 买入IBS

    elif amount1In > 0 and amount0Out > 0:


        usdt_amount = amount1In / 10**18

        ibs_amount = amount0Out / 10**18


        if ibs_amount >= 100:

            msg=f"""
🟢 IBS BUY

花费:
{usdt_amount:,.4f} USDT

买入:
{ibs_amount:,.4f} IBS

区块:
{log.blockNumber}

TX:
https://bscscan.com/tx/{tx}
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
