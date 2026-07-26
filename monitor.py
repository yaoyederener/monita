import os
import requests
from web3 import Web3

BOT_TOKEN=os.environ["BOT_TOKEN"]
CHAT_ID=os.environ["CHAT_ID"]

RPCS=[
"https://bsc-dataseed.binance.org",
"https://bsc-dataseed1.defibit.io",
"https://bsc-dataseed1.ninicoin.io",
"https://rpc.ankr.com/bsc",
]

TOKEN=Web3.to_checksum_address("0x255e746abb8d9acac00d6d023e5e63e3b8dfa7cd")
WALLET=Web3.to_checksum_address("0xed8b85788e15305c59de904fcaac0f2c9c4bd41b")
STATE="last_balance.txt"

w3=None
for rpc in RPCS:
    try:
        c=Web3(Web3.HTTPProvider(rpc,request_kwargs={"timeout":10}))
        if c.is_connected():
            w3=c
            print("Connected:",rpc)
            break
    except:
        pass
if w3 is None:
    raise Exception("RPC连接失败")

abi=[
{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
{"inputs":[],"name":"decimals","outputs":[{"type":"uint8"}],"stateMutability":"view","type":"function"},
{"inputs":[],"name":"symbol","outputs":[{"type":"string"}],"stateMutability":"view","type":"function"}]

c=w3.eth.contract(address=TOKEN,abi=abi)
dec=c.functions.decimals().call()
sym=c.functions.symbol().call()
bal=c.functions.balanceOf(WALLET).call()

old=None
if os.path.exists(STATE):
    try:
        old=int(open(STATE).read().strip())
    except:
        pass

if old is None:
    open(STATE,"w").write(str(bal))
    print("首次运行，已记录余额")
elif bal!=old:
    diff=(bal-old)/(10**dec)
    now=bal/(10**dec)
    msg=f"🚨余额变化\\n\\nToken:{sym}\\n变化:{diff:+,.6f}\\n当前:{now:,.6f}"
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                  json={"chat_id":CHAT_ID,"text":msg},timeout=20)
    open(STATE,"w").write(str(bal))
    print("Telegram Sent")
else:
    print("余额未变化")
