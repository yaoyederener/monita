"""🧪 极速预言家真钱小额测试（套利钱包当客户，自己付款、自己下注）。
指令写在 fast_test.run 第一行，推上去就自动跑一次：
    reg 2     注册 2 个 5 位纯数字 .ailgns（每个 5 DAI = 5 票）
    bet       十个数字各押 1 票（挤在同一期里，必中一个，用来走通发奖）
    all       先 reg 2 再 bet
需要 ARB_KEY。上线区块（FAST_START）之前不做任何事。
"""
import os
import random
import sys
import time

from eth_account import Account

from chains import rpc, send_tx
from fast_oracle import DAI, FAST_POOL, FAST_START, NAMES_AIL, PREFIX, ROUND
from reg_bot import abistr, num, pad, u256

PRICE = 5 * 10**18  # 5 位及以上纯数字：一年 5 DAI


def head():
    b = rpc("anubis", "eth_getBlockByNumber", ["latest", False])
    return int(b["number"], 16), int(b["timestamp"], 16)


def reg(acct, n):
    me = acct.address.lower()
    if num(DAI, "0xdd62ed3e" + pad(me) + pad(NAMES_AIL)) < n * PRICE:
        print("授权域名合约", send_tx("anubis", acct, DAI, "0x095ea7b3" + pad(NAMES_AIL) + u256(n * PRICE), "授权域名合约"))
    for _ in range(n):
        while True:
            name = str(random.randint(10000, 99999))
            if num(NAMES_AIL, "0xaeb8ce9b" + u256(0x20) + abistr(name)):
                break
        h = send_tx("anubis", acct, NAMES_AIL, "0xd868a84f" + u256(0xa0) + pad(me) + u256(1) + pad(DAI) + u256(PRICE) + abistr(name), f"注册 {name}")
        print(f"✅ 注册 {name}.ailgns 5 DAI = 5 票 {h}")


def bet(acct):
    blk, ts = head()
    left = ROUND - ts % ROUND
    if left < 200:  # 这一期剩的时间不够十笔都落进去，等下一期
        print(f"本期只剩 {left} 秒，等下一期")
        time.sleep(left + 8)
        blk, ts = head()
    r = ts // ROUND
    print(f"第 {r} 期（{time.strftime('%H:%M', time.gmtime(r * ROUND + 8 * 3600))} 到 {time.strftime('%H:%M', time.gmtime((r + 1) * ROUND + 8 * 3600))} 北京时间）十个数字各押 1 票")
    for d in range(10):
        memo = (PREFIX + f"{d}:1").encode().hex()
        h = send_tx("anubis", acct, DAI, "0xa9059cbb" + pad(FAST_POOL) + u256(0) + memo, f"押 {d}")
        print(f"  押 {d} × 1 票 {h}")
    _, ts2 = head()
    print("✅ 十笔都发完" + ("，全在同一期" if ts2 // ROUND == r else "，⚠️ 有几笔落到了下一期"))


def main():
    cmd = (open("fast_test.run").readline().split("#")[0].split() if os.path.exists("fast_test.run") else []) or ["none"]
    acct = Account.from_key(os.environ["ARB_KEY"])
    blk, _ = head()
    bal = int(rpc("anubis", "eth_getBalance", [acct.address, "latest"]), 16) / 1e18
    print("测试钱包", acct.address.lower(), f"DAI {bal:.2f}", "区块", blk, "上线区块", FAST_START, "指令", cmd)
    if not FAST_START or blk < FAST_START:
        print("还没到上线区块，不做")
        return
    n = int(cmd[1]) if len(cmd) > 1 else 2
    if cmd[0] in ("reg", "all"):
        if bal < n * 5 + 1:
            sys.exit(f"DAI 不够：要 {n * 5 + 1}")
        reg(acct, n)
    if cmd[0] in ("bet", "all"):
        bet(acct)


if __name__ == "__main__":
    main()
