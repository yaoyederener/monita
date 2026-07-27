import os
import requests


API_KEY = os.environ["BSCSCAN_KEY"]

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]


CHAIN_ID = 56


PAIR = "0x2a4B99A9c4544D35e8D266111c50B67fEA01d53d"


# PancakeSwap V2 Swap event
SWAP_TOPIC = (
    "0xd78ad95fa46c994b6551d0da85fc275fe613ce3766c1e7c0f8f8b7e6b6a6e5e"
)


API_URL = "https://api.etherscan.io/v2/api"



def send_telegram(msg):

    try:

        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": msg
            },
            timeout=20
        )

    except Exception as e:

        print(
            "Telegram错误:",
            e
        )



def get_swap_logs():


    params = {

        "chainid": CHAIN_ID,

        "module": "logs",

        "action": "getLogs",

        "address": PAIR,

        "topic0": SWAP_TOPIC,

        "fromBlock": "latest-100",

        "toBlock": "latest",

        "apikey": API_KEY

    }


    r = requests.get(
        API_URL,
        params=params,
        timeout=30
    )


    data = r.json()


    if data.get("status") != "1":

        print(
            "API返回:",
            data
        )

        return []


    return data["result"]



print("Etherscan V2 BSC Connected")



logs = get_swap_logs()



print(
    "发现Swap:",
    len(logs)
)



for log in logs:


    data = log["data"][2:]


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


    tx = log["transactionHash"]


    msg = None



    # token0=IBS token1=USDT

    # IBS卖出

    if amount0In > 0 and amount1Out > 0:


        ibs = amount0In / 10**18

        usdt = amount1Out / 10**18


        if ibs >= 100:

            msg = f"""
🔴 IBS SELL

卖出:
{ibs:,.4f} IBS

收到:
{usdt:,.4f} USDT

区块:
{log['blockNumber']}

TX:
https://bscscan.com/tx/{tx}
"""



    # 买入IBS

    elif amount1In > 0 and amount0Out > 0:


        usdt = amount1In / 10**18

        ibs = amount0Out / 10**18


        if ibs >= 100:

            msg = f"""
🟢 IBS BUY

支付:
{usdt:,.4f} USDT

买入:
{ibs:,.4f} IBS

区块:
{log['blockNumber']}

TX:
https://bscscan.com/tx/{tx}
"""



    if msg:

        print(msg)

        send_telegram(msg)



print("完成")
