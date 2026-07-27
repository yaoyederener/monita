import os
import requests
from web3 import Web3
from web3.middleware import geth_poa_middleware


BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]


RPC = "https://bsc-dataseed.bnbchain.org"


PAIR = Web3.to_checksum_address(
    "0x2a4B99A9c4544D35e8D266111c50B67fEA01d53d"
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



# 获取池子 token0 / token1

token0 = Web3.to_checksum_address(
    w3.eth.call(
        {
            "to": PAIR,
            "data": "0x0dfe1681"
        }
    )[-20:].hex()
)


token1 = Web3.to_checksum_address(
    w3.eth.call(
        {
            "to": PAIR,
            "data": "0xd21220a7"
        }
    )[-20:].hex()
)



print("token0:", token0)
print("token1:", token1)



SWAP_TOPIC = Web3.keccak(
    text="Swap(address,uint256,uint256,uint256,uint256,address)"
).hex()



latest = w3.eth.block_number


# 最近50区块

start = latest - 50


print(
    f"扫描 {start}-{latest}"
)



logs = []


# 每5个区块请求一次

for block in range(
    start,
    latest + 1,
    5
):

    end = min(
        block + 4,
        latest
    )


    try:

        result = w3.eth.get_logs(
            {
                "fromBlock": block,
                "toBlock": end,
                "address": PAIR,
                "topics": [
                    SWAP_TOPIC
                ]
            }
        )

        logs.extend(result)


    except Exception as e:

        print(
            f"RPC错误 {block}-{end}:",
            e
        )



print(
    "发现Swap:",
    len(logs)
)



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



    # token0 = IBS

    if token0.lower() == IBS.lower():


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

支付:
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
                "chat_id": CHAT_ID,
                "text": msg
            },
            timeout=20
        )



print("完成")
