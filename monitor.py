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


# 只监控最新两个区块
start_block = latest - 1


print(
    f"扫描区块 {start_block}-{latest}"
)


for block_number in range(start_block, latest + 1):

    try:

        block = w3.eth.get_block(
            block_number,
            full_transactions=True
        )

    except Exception as e:

        print("读取区块失败:", e)
        continue


    print(
        "交易数量:",
        len(block.transactions)
    )


    for tx in block.transactions:

        try:

            receipt = w3.eth.get_transaction_receipt(
                tx.hash
            )

        except:

            continue


        for log in receipt.logs:


            # 调试：发现 IBS 日志
            if log.address.lower() == TOKEN.lower():

                print(
                    "发现IBS日志:",
                    tx.hash.hex()
                )


            if log.address.lower() != TOKEN.lower():
                continue


            if log.topics[0].hex() != TRANSFER_TOPIC:
                continue



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


            txhash = tx.hash.hex()


            msg = None



            # Mint

            if (
                from_addr.lower() == ZERO
                and
                to_addr.lower() == WALLET.lower()
            ):

                msg = f"""
🚨 IBS 增发 Mint

数量:
{amount:,.6f} IBS

接收:
{WALLET}

区块:
{block_number}

交易:
https://bscscan.com/tx/{txhash}
"""


            # 转入

            elif to_addr.lower() == WALLET.lower():

                msg = f"""
🟢 IBS 转入

数量:
{amount:,.6f} IBS

来源:
{from_addr}

交易:
https://bscscan.com/tx/{txhash}
"""


            # 转出

            elif from_addr.lower() == WALLET.lower():

                msg = f"""
🔴 IBS 转出

数量:
{amount:,.6f} IBS

目标:
{to_addr}

交易:
https://bscscan.com/tx/{txhash}
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

                print(
                    "Telegram:",
                    r.text
                )


print("完成")
