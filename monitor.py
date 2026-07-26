import os
import requests
from web3 import Web3


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


w3 = Web3(
    Web3.HTTPProvider(
        RPC,
        request_kwargs={"timeout":30}
    )
)


if not w3.is_connected():
    raise Exception("RPC连接失败")


TRANSFER_TOPIC = Web3.keccak(
    text="Transfer(address,address,uint256)"
).hex()


ZERO = "0x0000000000000000000000000000000000000000"


latest = w3.eth.block_number


if os.path.exists(STATE):

    with open(STATE) as f:
        last = int(f.read())

else:

    last = latest - 2


print(
    f"扫描区块 {last+1}-{latest}"
)



for block_num in range(last+1, latest+1):

    block = w3.eth.get_block(
        block_num,
        full_transactions=True
    )


    for tx in block.transactions:


        try:

            receipt = w3.eth.get_transaction_receipt(
                tx.hash
            )


        except:
            continue



        for log in receipt.logs:


            if (
                log.address.lower()
                != TOKEN.lower()
            ):
                continue


            if (
                log.topics[0].hex()
                != TRANSFER_TOPIC
            ):
                continue



            from_addr = Web3.to_checksum_address(
                "0x"+log.topics[1].hex()[-40:]
            )

            to_addr = Web3.to_checksum_address(
                "0x"+log.topics[2].hex()[-40:]
            )


            amount = (
                int(log.data.hex(),16)
                /
                10**18
            )


            msg = None


            if (
                from_addr == ZERO
                and
                to_addr == WALLET
            ):

                msg=f"""
🚨 IBS Mint 增发

数量:
{amount:,.6f} IBS

接收:
{WALLET}

区块:
{block_num}

Tx:
https://bscscan.com/tx/{tx.hash.hex()}
"""



            elif from_addr == WALLET:


                msg=f"""
🔴 IBS 转出

数量:
{amount:,.6f} IBS

目标:
{to_addr}

区块:
{block_num}

Tx:
https://bscscan.com/tx/{tx.hash.hex()}
"""



            elif to_addr == WALLET:


                msg=f"""
🟢 IBS 转入

数量:
{amount:,.6f} IBS

来源:
{from_addr}

区块:
{block_num}

Tx:
https://bscscan.com/tx/{tx.hash.hex()}
"""



            if msg:

                print(msg)


                r=requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id":CHAT_ID,
                        "text":msg
                    },
                    timeout=20
                )

                print(r.text)



with open(STATE,"w") as f:
    f.write(str(latest))


print("完成")
