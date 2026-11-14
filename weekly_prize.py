"""
🏆 OTC 手续费周榜发奖（站长 2026-11-15 定，规则在网站 lib/weekly.js）

只做两件事：
  ① 叫网站结算刚结束的那一周、核对谁烧了创世名：GET /api/weekly?pay=1&key=BACKUP_KEY
  ② 把网站说「可以发」的奖，从【周榜专用钱包】转 DAI 给上榜的人，然后告诉网站记为已发。

钥匙：WEEKLY_KEY = 周榜专用钱包的私钥（这个钱包只放周榜的钱；站长自己建、自己放进 GitHub Secret）。
      BACKUP_KEY = 读写网站发奖记录用的口令。
没有 WEEKLY_KEY：只做 ①，不发钱。这个脚本碰不到别的钱包，也不改任何合约。

绝不重复转账（三道）：
  1. 转账前先到网站把这一笔「锁住」（action=lock）。锁不上（已发 / 已锁 / 没烧）就跳过。
  2. 锁上以后才转；转完马上把交易哈希写进 data/weekly_prize.json，再告诉网站记为已发。
  3. 网站记账没成：下一轮只补记账，不再转。锁了但没转成（比如中途断了）：这一笔就一直锁着，网站会提醒站长人工核对，脚本不会自己重发。
"""
import argparse
import json
import os
import urllib.request

from eth_account import Account

from chains import rpc, send_tx

SITE = "https://ailgns.dev/api/weekly"
DAI = "0x83fd06f0846d9d90b3016bf670efe2e0b11cde14"  # Anubis 的 DAI：原生币的 ERC20 外壳，transfer 转的就是钱包里的 DAI
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "weekly_prize.json")
TX = "https://anubisscan.io/tx/"
GAS_KEEP = 5 * 10**17  # 转完以后钱包里至少还剩 0.5 DAI 付 Gas


def pad(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def http(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, headers={"content-type": "application/json", "user-agent": "ailgns-keeper"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def wei(amt):
    return int(round(float(amt) * 100)) * 10**16  # 网站给的是两位小数的 DAI


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    dry = ap.parse_args().dry_run
    key = os.environ.get("BACKUP_KEY")
    if not key:
        print("没有 BACKUP_KEY，周榜这一轮不跑")
        return
    st = json.load(open(STATE)) if os.path.exists(STATE) else {"sent": {}}

    def save():
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as f:
            json.dump(st, f)

    try:
        r = http(SITE + "?pay=1&key=" + key)
    except Exception as e:
        print(f"周榜接口没通（还没上线或网络问题），这一轮不跑：{e}")
        return
    if r.get("error"):
        print("网站说：" + str(r["error"]))
    if r.get("settled"):
        print(f"周榜：{r['settled']} 这一周已结算")
    for x in r.get("stuck") or []:
        print(f"⚠️ 有一笔卡在「发放中」：{x['a']} {x['amt']} DAI（{x['id']}）——不会自动重发，要人工到链上核对")
    # 上一轮转了账、但网站没记上的：只补记账
    for pid, h in list(st["sent"].items()):
        if h and not dry:
            try:
                http(SITE, {"action": "paid", "id": pid, "tx": h, "key": key})
                st.setdefault("ok", {})[pid] = h  # 网站记上了：挪到「办完的」里，下轮不用再报
                del st["sent"][pid]
                save()
            except Exception as e:
                print(f"⚠️ 补记账没成（下轮再试）：{pid} {e}")
    todo = r.get("pay") or []
    pool = (r.get("pool") or "").lower()
    if not todo:
        print("周榜：现在没有要发的奖")
        return
    acct = Account.from_key(os.environ["WEEKLY_KEY"]) if os.environ.get("WEEKLY_KEY") else None
    if not acct:
        print(f"周榜：有 {len(todo)} 笔可以发，但没有 WEEKLY_KEY（周榜钱包还没配好），这一轮不发")
        return
    if not pool or acct.address.lower() != pool:
        print(f"⚠️ WEEKLY_KEY 对应的钱包 {acct.address} 和网站登记的周榜钱包 {pool or '（没填）'} 不是同一个，不发")
        return
    for p in todo:
        pid, to, n = p["id"], p["a"].lower(), wei(p["amt"])
        if pid in st["sent"] or pid in st.get("ok", {}):
            print(f"{pid} 本机记着已经锁过 / 转过（{st['sent'].get(pid) or st.get('ok', {}).get(pid) or '锁了没转成，等人工核对'}），跳过")
            continue
        bal = int(rpc("anubis", "eth_getBalance", [acct.address, "latest"]), 16)
        if n <= 0 or bal < n + GAS_KEEP:
            print(f"⚠️ 周榜钱包余额 {bal / 1e18:.4f} DAI，不够发 {p['amt']} DAI 给 {to}，这笔先不发")
            continue
        if dry:
            print(f"（dry-run）会从周榜钱包转 {p['amt']} DAI 给 {to}（{pid}）")
            continue
        lk = http(SITE, {"action": "lock", "id": pid, "key": key})
        if not lk.get("ok") or str(lk.get("a", "")).lower() != to or wei(lk.get("amt", 0)) != n:
            print(f"{pid} 网站没给锁（{lk}），跳过")
            continue
        st["sent"][pid] = ""  # 锁上了、正要转
        save()
        h = send_tx("anubis", acct, DAI, "0xa9059cbb" + pad(to) + f"{n:064x}", "周榜奖金")
        st["sent"][pid] = h
        save()  # 钱一转出去马上存，后面出错也不会忘
        print(f"周榜奖金：{p['amt']} DAI → {to} {TX}{h}")
        try:
            http(SITE, {"action": "paid", "id": pid, "tx": h, "key": key})
            st.setdefault("ok", {})[pid] = h
            del st["sent"][pid]
            save()
        except Exception as e:
            print(f"⚠️ 网站记账没成（下轮补记，不会重发）：{e}")


if __name__ == "__main__":
    main()
