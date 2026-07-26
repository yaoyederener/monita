import os
import requests


BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
API_KEY = os.environ["BSCSCAN_KEY"]


TOKEN = "0x255e746abb8d9acac00d6d023e5e63e3b8dfa7cd"

WALLET = "0xed8b85788e15305c59de904fcaac0f2c9c4bd41b"

STATE = "last_tx.txt"


url = "https://api.bscscan.com/api"


params = {

    "module":"account",
    "action":"tokentx",

    "contractaddress":TOKEN,

    "address":WALLET,

    "page":1,
    "offset":20,

    "sort":"desc",

    "apikey":API_KEY

}


r = requests.get(
    url,
    params=params,
    timeout=20
)


data = r.json()


if data["status"] != "1":

    print(data)

    exit()



txs = data["result"]


if os.path.exists(STATE):

    with open(STATE) as f:
        last_tx = f.read().strip()

else:

    last_tx = ""


for tx in reversed(txs):


    txhash = tx["hash"]


    if txhash == last_tx:

        continue



    from_addr = tx["from"].lower()

    to_addr = tx["to"].lower()


    amount = (
        int(tx["value"])
        /
        10**18
    )



    if from_addr == "0x0000000000000000000000000000000000000000":

        msg = f"""
🚨 IBS Mint 增发

数量:
{amount:,.6f} IBS

接收:
{to_addr}

交易:
https://bscscan.com/tx/{txhash}
"""


    elif from_addr == WALLET.lower():

        msg = f"""
🔴 IBS 转出

数量:
{amount:,.6f} IBS

目标:
{to_addr}

交易:
https://bscscan.com/tx/{txhash}
"""


    else:

        msg = f"""
🟢 IBS 转入

数量:
{amount:,.6f} IBS

来源:
{from_addr}

交易:
https://bscscan.com/tx/{txhash}
"""



    print(msg)


    requests.post(

        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",

        json={
            "chat_id":CHAT_ID,
            "text":msg
        }

    )


with open(STATE,"w") as f:

    f.write(txs[-1]["hash"])


print("完成")
