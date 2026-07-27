import os
import requests
from web3 import Web3
from web3.middleware import geth_poa_middleware


BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]


RPC = "https://bsc-dataseed.binance.org"


PAIR = Web3.to_checksum_address(
    "0x2a4B99A9c4544D35e8D266111c50B67fEA01d53d"
)


IBS = "0x255e746abb8d9acac00d6d023e5e63e3b8dfa7cd"


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


SWAP_TOPIC = Web3.keccak(
    text="Swap(address,uint256,uint256,uint256,uint256,address)"
).hex()



latest = w3.eth.block_number


start = latest - 100


print(
    f"扫描 {start}-{latest}"
)



for block in range(start, latest+1):

    try:

        logs = w3.eth.get_logs({

            "fromBlock": block,

            "toBlock": block,

            "address": PAIR,

            "topics":[
                SWAP_TOPIC
            ]

        })


    except Exception as e:

        print(
            "错误:",
            block,
            e
        )

        continue



    for log in logs:

        data = log.data.hex()[2:]


        amount0In = int(data[0:64],16)
        amount1In = int(data[64:128],16)
        amount0Out = int(data[128:192],16)
        amount1Out = int(data[192:256],16)


        tx = log.transactionHash.hex()


        msg = None


        # IBS卖出

        if amount0In > 0 and amount1Out > 0:

            ibs = amount0In / 10**18
            usdt = amount1Out / 10**18


            if ibs >= 100:

                msg=f"""
🔴 IBS SELL

卖出:
{ibs:,.4f} IBS

收到:
{usdt:,.4f} USDT

TX:
https://bscscan.com/tx/{tx}
"""



        # IBS买入

        elif amount1In > 0 and amount0Out > 0:

            usdt = amount1In / 10**18
            ibs = amount0Out / 10**18


            if ibs >= 100:

                msg=f"""
🟢 IBS BUY

支付:
{usdt:,.4f} USDT

买入:
{ibs:,.4f} IBS

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
                }
            )


print("完成")
