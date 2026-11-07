"""🤖 AiLGNS 代注册机器人（站长 2026-11-08 定：批量注册第 2 步「授权机器人批量点击」）
客户在网页上：授权 DAI 给本钱包（只授权这次的总额）+ 签名委托 → 网站存成待办。
本脚本每 5 分钟：取待办 → 每个名字：先查还能不能注册 → 用客户的授权 transferFrom 收这个名字的钱 → 本钱包 register(名字, 归客户)
→ 注册失败就把钱原路退回 → 全部做完回报网站（网站发站内信）→ 打「批量注册」标记（收入 10% 运营 / 90% 极速资金池）。
本钱包只放 Gas，不碰任何合约管理权限。需要：REG_KEY（本钱包私钥）、BACKUP_KEY（读写网站待办）。
    python reg_bot.py --dry-run
"""
import argparse
import json
import os
import urllib.request
from eth_account import Account
from eth_utils import keccak
from chains import rpc, send_tx

DAI = "0x83fd06f0846d9d90b3016bf670efe2e0b11cde14"
NAMES = "0xbc408e9d7350967e7247c917a086cb4ece0ed988"
SITE = "https://ailgns.dev/api/reg-order"
MARK = "https://ailgns.dev/api/batch-mark"
MAX_U = (1 << 256) - 1


def pad(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def u256(v):
    return f"{v:064x}"


def abistr(s):  # ABI string 尾部：长度 + 内容补齐 32 字节
    h = s.encode("utf-8").hex()
    return u256(len(h) // 2) + h + "0" * ((64 - len(h) % 64) % 64)


def call(to, data):
    return rpc("anubis", "eth_call", [{"to": to, "data": data}, "latest"])


def num(to, data):
    return int(call(to, data), 16)


def http(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, headers={"content-type": "application/json", "user-agent": "ailgns-keeper"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def chars(s):
    return len(s)


def cn(s):
    return any("一" <= c <= "鿿" for c in s)


def year_price(s):  # 和网站 names.js 的 yearPrice 一样
    n = chars(s)
    if cn(s):
        return 50 if n == 2 else 5
    return 640 if n == 3 else 160 if n == 4 else 5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    dry = ap.parse_args().dry_run
    key = os.environ.get("BACKUP_KEY")
    acct = Account.from_key(os.environ["REG_KEY"]) if os.environ.get("REG_KEY") else None
    if not key or not acct:
        print("缺 REG_KEY / BACKUP_KEY，跳过")
        return
    me = acct.address.lower()
    gas = int(rpc("anubis", "eth_getBalance", [acct.address, "latest"]), 16) / 1e18
    print(f"机器人钱包 {me} Gas {gas:.4f}")
    orders = http(f"{SITE}?key={key}").get("list") or []
    if not orders:
        print("没有待办")
        return
    if gas < 0.01:
        print("⚠️ Gas 快没了，这轮不做")
        return
    for o in orders[:20]:
        a, done, fail = o["a"].lower(), [], []
        print(f"委托 {o['id']} {a} {len(o['names'])} 个")
        for n in o["names"]:
            cost = year_price(n) * 10**18
            try:
                if num(NAMES, "0xaeb8ce9b" + u256(0x20) + abistr(n)) == 0:
                    fail.append(f"{n}（被人抢先了）"); continue
                allow = num(DAI, "0xdd62ed3e" + pad(a) + pad(me)); have = num(DAI, "0x70a08231" + pad(a))
                if allow < cost or have < cost:
                    fail.append(f"{n}（{'授权' if allow < cost else 'DAI'}不够）"); continue
                if dry:
                    print(f"  dry {n} {cost / 1e18} DAI"); done.append(n); continue
                send_tx("anubis", acct, DAI, "0x23b872dd" + pad(a) + pad(me) + u256(cost), f"收 {n} 的款")
                try:
                    if num(DAI, "0xdd62ed3e" + pad(me) + pad(NAMES)) < cost:
                        send_tx("anubis", acct, DAI, "0x095ea7b3" + pad(NAMES) + u256(MAX_U), "授权域名合约")
                    # register(label, owner, years, token, maxAmount)
                    h = send_tx("anubis", acct, NAMES, "0xd868a84f" + u256(0xa0) + pad(a) + u256(1) + pad(DAI) + u256(cost) + abistr(n), f"注册 {n}")
                    print(f"  ✅ {n} → {a} {h}"); done.append(n)
                except Exception as e:
                    print(f"  ❌ {n} 注册失败，退款：{e}")
                    try:
                        send_tx("anubis", acct, DAI, "0xa9059cbb" + pad(a) + u256(cost), f"退 {n} 的款")
                        fail.append(f"{n}（注册失败，已退款）")
                    except Exception as e2:
                        print(f"  ⚠️ 退款也失败：{e2}"); fail.append(f"{n}（注册失败，退款待人工）")
            except Exception as e:
                print(f"  ❌ {n} 出错：{e}"); fail.append(f"{n}（出错，没扣钱）")
        if dry:
            continue
        try:
            http(SITE, {"action": "result", "key": key, "id": o["id"], "done": done, "fail": fail})
            if done:
                http(MARK, {"a": a, "names": done})
        except Exception as e:
            print(f"回报网站失败：{e}")
        print(f"委托 {o['id']} 完成：成功 {len(done)} 失败 {len(fail)}")


if __name__ == "__main__":
    main()
