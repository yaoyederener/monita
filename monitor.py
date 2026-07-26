import os
import requests
from web3 import Web3
from web3.middleware import geth_poa_middleware


BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

RPC = "https://bsc-dataseed1.binance.org"


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
        request_kwargs={"timeout":20}
    )
)

w3.middleware_onion.inject(
    geth_poa_middleware,
    layer=0
)


if not w3.is_connected():
    raise Exception("RPC failed")


latest = w3.eth.block_number


if os.path.exists(STATE):

    with open(STATE) as f:
        last = int(f.read())

else:

    last = latest - 1


if last >= latest:
    print("没有新区块")
    exit()


print(
    f"扫描 {last+1} - {latest}"
)


TRANSFER = Web3.keccak(
    text="Transfer(address,address,uint256)"
).hex()


wallet_topic = (
    "0x000000000000000000000000"
    +
    WALLET[2:].lower()
)


topics_list = [

    # 转入钱包
    [
        TRANSFER,
        None,
        wallet_topic
    ],

    # 钱包转出
    [
        TRANSFER,
        wallet_topic,
        None
    ]

]


for topics in topics_list:

    try:

        logs = w3.eth.get_logs(
            {
                "address": TOKEN,
                "fromBlock": last + 1,
                "toBlock": latest,
                "topics": topics
            }
        )


    except Exception as e:

        print("RPC错误:", e)
        continue



    for log in logs:


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


        tx = log["transactionHash"].hex()


        msg = None


        # Mint

        if (
            from_addr ==
            "0x0000000000000000000000000000000000000000"
            and
            to_addr == WALLET
        ):

            msg = f"""
🚨 IBS Mint 增发

数量:
{amount:,.6f} IBS

钱包:
{WALLET}

交易:
https://bscscan.com/tx/{tx}
"""


        elif to_addr == WALLET:

            msg = f"""
🟢 IBS 转入

数量:
{amount:,.6f} IBS

来源:
{from_addr}

交易:
https://bscscan.com/tx/{tx}
"""


        elif from_addr == WALLET:

            msg = f"""
🔴 IBS 转出

数量:
{amount:,.6f} IBS

目标:
{to_addr}

交易:
https://bscscan.com/tx/{tx}
"""



        if msg:

            print(msg)

            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": CHAT_ID,
                    "text": msg
                },
                timeout=20
            )

            print(r.text)



with open(STATE,"w") as f:
    f.write(str(latest))


print("完成")
