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


STATE = "last_block.txt"


# 连接 BSC

w3 = Web3(
    Web3.HTTPProvider(
        RPC,
        request_kwargs={
            "timeout": 30
        }
    )
)


# BSC 必须
w3.middleware_onion.inject(
    geth_poa_middleware,
    layer=0
)


if not w3.is_connected():
    raise Exception("RPC连接失败")


print("BSC Connected")


TRANSFER_TOPIC = Web3.keccak(
    text="Transfer(address,address,uint256)"
).hex()


ZERO = Web3.to_checksum_address(
    "0x0000000000000000000000000000000000000000"
)


latest = w3.eth.block_number


# 读取区块

if os.path.exists(STATE):

    with open(STATE) as f:
        last_block = int(f.read().strip())

else:

    last_block = latest - 5



if last_block >= latest:

    print("没有新区块")
    exit()



print(
    f"扫描区块 {last_block+1}-{latest}"
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
            "区块读取失败:",
            e
        )

        continue



    for tx in block.transactions:


        try:

            receipt = w3.eth.get_transaction_receipt(
                tx.hash
            )


        except Exception:

            continue



        for log in receipt.logs:


            # 只看 IBS

            if log.address.lower() != TOKEN.lower():

                continue



            # 只看 Transfer

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



            message = None



            # 增发

            if (
                from_addr == ZERO
                and
                to_addr == WALLET
            ):


                message = f"""
🚨 IBS 增发 Mint

数量:
{amount:,.6f} IBS

接收钱包:
{WALLET}

区块:
{block_number}

交易:
https://bscscan.com/tx/{tx.hash.hex()}
"""



            # 钱包收到

            elif to_addr == WALLET:


                message = f"""
🟢 IBS 转入

数量:
{amount:,.6f} IBS

来源:
{from_addr}

区块:
{block_number}

交易:
https://bscscan.com/tx/{tx.hash.hex()}
"""



            # 钱包卖出/转出

            elif from_addr == WALLET:


                message = f"""
🔴 IBS 转出

数量:
{amount:,.6f} IBS

目标:
{to_addr}

区块:
{block_number}

交易:
https://bscscan.com/tx/{tx.hash.hex()}
"""



            if message:


                print(message)


                try:

                    r = requests.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": CHAT_ID,
                            "text": message
                        },
                        timeout=20
                    )


                    print(
                        "Telegram:",
                        r.text
                    )


                except Exception as e:

                    print(
                        "Telegram错误:",
                        e
                    )



# 保存区块

with open(STATE,"w") as f:

    f.write(
        str(latest)
    )


print("完成")
