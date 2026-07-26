import os
import requests
from web3 import Web3
from web3.middleware import geth_poa_middleware


BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]


RPC = "https://bsc-dataseed1.binance.org"


IBS = Web3.to_checksum_address(
    "0x255e746abb8d9acac00d6d023e5e63e3b8dfa7cd"
)

PAIR = Web3.to_checksum_address(
    "0x2a4B99A9c4544D35e8D266111c50B67fEA01d53d"
)

TARGET = Web3.to_checksum_address(
    "0xed8b85788E15305c59De904fCAAC0F2c9c4Bd41b"
)

USDT = Web3.to_checksum_address(
    "0x55d398326f99059fF775485246999027B3197955"
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
    raise Exception("BSC连接失败")


print("BSC Connected")


def send(msg):

    print(msg)

    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": msg
        },
        timeout=20
    )



# 获取token顺序

PAIR_ABI = [
{
"constant":True,
"inputs":[],
"name":"token0",
"outputs":[{"name":"","type":"address"}],
"type":"function"
},
{
"constant":True,
"inputs":[],
"name":"token1",
"outputs":[{"name":"","type":"address"}],
"type":"function"
}
]


pair = w3.eth.contract(
    address=PAIR,
    abi=PAIR_ABI
)


token0 = Web3.to_checksum_address(
    pair.functions.token0().call()
)

token1 = Web3.to_checksum_address(
    pair.functions.token1().call()
)


print("token0:",token0)
print("token1:",token1)



latest = w3.eth.block_number



if os.path.exists(STATE):

    with open(STATE) as f:
        last=int(f.read())

else:

    last=latest-5



if latest-last > 20:
    last=latest-20



print(
    f"扫描 {last+1}-{latest}"
)



# Transfer事件

TRANSFER = Web3.keccak(
    text="Transfer(address,address,uint256)"
).hex()


# Swap事件

SWAP = Web3.keccak(
    text="Swap(address,uint256,uint256,uint256,uint256,address)"
).hex()



ZERO="0x0000000000000000000000000000000000000000"

DEAD="0x000000000000000000000000000000000000dead"



# ===================
# IBS Transfer
# ===================


logs=w3.eth.get_logs({

"address":IBS,

"fromBlock":last+1,

"toBlock":latest,

"topics":[TRANSFER]

})


for log in logs:


    frm="0x"+log["topics"][1].hex()[-40:]

    to="0x"+log["topics"][2].hex()[-40:]


    amount=int(log["data"].hex(),16)/10**18


    tx=log["transactionHash"].hex()



    if frm.lower()==ZERO:

        send(f"""
🚨 IBS Mint

数量:
{amount:,.6f} IBS

接收:
{to}

TX:
https://bscscan.com/tx/{tx}
""")


    elif to.lower()==DEAD:

        send(f"""
🔥 IBS Burn

销毁:
{amount:,.6f} IBS

来源:
{frm}

TX:
https://bscscan.com/tx/{tx}
""")



# ===================
# PancakeSwap Swap
# ===================


swap_logs=w3.eth.get_logs({

"address":PAIR,

"fromBlock":last+1,

"toBlock":latest,

"topics":[SWAP]

})



for log in swap_logs:


    data=log["data"].hex()


    # 去掉0x
    data=data[2:]


    values=[]

    for i in range(0,256,64):

        values.append(
            int(data[i:i+64],16)
        )


    amount0in=values[0]
    amount1in=values[1]
    amount0out=values[2]
    amount1out=values[3]


    tx=log["transactionHash"].hex()



    # IBS 是 token0

    if token0.lower()==IBS.lower():

        ibs_in=amount0in
        ibs_out=amount0out

        usdt_in=amount1in
        usdt_out=amount1out


    else:

        ibs_in=amount1in
        ibs_out=amount1out

        usdt_in=amount0in
        usdt_out=amount0out



    if ibs_in>0 and usdt_out>0:

        send(f"""
🔴 IBS SELL

卖出:
{ibs_in/1e18:,.6f} IBS

收到:
{usdt_out/1e18:,.6f} USDT

TX:
https://bscscan.com/tx/{tx}
""")


    elif usdt_in>0 and ibs_out>0:

        send(f"""
🟢 IBS BUYBACK

花费:
{usdt_in/1e18:,.6f} USDT

买入:
{ibs_out/1e18:,.6f} IBS

TX:
https://bscscan.com/tx/{tx}
""")


with open(STATE,"w") as f:
    f.write(str(latest))


print("完成")
