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
    "0xed8b85788E15305c59De904fCAAC0F2c9c4Bd41b"
)


STATE_FILE = "last_block.txt"


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



# 读取上次区块

if os.path.exists(STATE_FILE):

    with open(STATE_FILE) as f:
        last_block = int(f.read().strip())

else:

    # 第一次运行，只看最近5个区块
    last_block = latest - 5



# 防止一次跑太多

if latest - last_block > 500:

    last_block = latest - 500



print(
    f"扫描区块 {last_block+1} - {latest}"
)



for block_number in range(
    last_block + 1,
    latest + 1
):


    try:

        block = w3.eth.get_block(
            block_number,
            full_transactions=True
        )

    except Exception as e:

        print(
            "读取区块失败:",
            e
        )

        continue



    print(
        "区块:",
        block_number,
        "交易:",
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
🚨 IBS Mint 增发

数量:
{amount:,.6f} IBS

接收:
{WALLET}

区块:
{block_number}

交易:
https://bscscan.com/tx/{txhash}
"""



            # 钱包收到

            elif to_addr.lower() == WALLET.lower():


                msg = f"""
🟢 IBS 转入

数量:
{amount:,.6f} IBS

来源:
{from_addr}

区块:
{block_number}

交易:
https://bscscan.com/tx/{txhash}
"""



            # 钱包转出

            elif from_addr.lower() == WALLET.lower():


                msg = f"""
🔴 IBS 转出

数量:
{amount:,.6f} IBS

目标:
{to_addr}

区块:
{block_number}

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



# 保存最新区块

with open(STATE_FILE,"w") as f:

    f.write(
        str(latest)
    )


print("完成")
