"""🧪 代注册全流程小额测试：用套利钱包当「客户」：授权 5 DAI 给机器人 → 签名委托 → POST 网站 → 再跑 reg_bot.py 看能否注册成功。
    python reg_test.py   需要 ARB_KEY（当客户）
"""
import json
import os
import random
import urllib.request
from datetime import datetime, timedelta, timezone
from eth_account import Account
from eth_account.messages import encode_defunct
from chains import rpc, send_tx
from reg_bot import DAI, NAMES, pad, u256, abistr, num, http, SITE

REG = "0x21425f852a1e0fb798fb9ebeb288def27bc0f2d1"
acct = Account.from_key(os.environ["ARB_KEY"])
a = acct.address.lower()
# 找一个还能注册的 5 位数
while True:
    n = str(random.randint(10000, 99999))
    if num(NAMES, "0xaeb8ce9b" + u256(0x20) + abistr(n)):
        break
total = 5
day = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")
print("客户", a, "名字", n, "日期", day)
if num(DAI, "0xdd62ed3e" + pad(a) + pad(REG)) < total * 10**18:
    print("授权", send_tx("anubis", acct, DAI, "0x095ea7b3" + pad(REG) + u256(total * 10**18), "授权机器人"))
text = "AiLGNS 代注册委托\n请机器人帮我注册（各 1 年，DAI 付款）：" + n + "\n共 " + str(total) + " DAI\n我的地址：" + a + "\n日期：" + day
sig = Account.sign_message(encode_defunct(text=text), acct.key).signature.hex()
r = http(SITE, {"a": a, "names": [n], "day": day, "sig": sig if sig.startswith("0x") else "0x" + sig})
print("下单", r)
assert r.get("id"), r
