"""
🏆 OTC 手续费周榜发奖（站长 2026-11-15 定，规则在网站 lib/weekly.js）

只做两件事：
  ① 叫网站结算刚结束的那一周、核对谁烧了创世名：GET /api/weekly?pay=1&key=BACKUP_KEY
  ② 把网站说「可以发」的奖发出去。站长定「发 AiLGNS」：每个人分到的是一个 DAI 金额，
     发的时候用【周榜专用钱包】里的 DAI 在本站 OTC 吃最便宜的卖单买成 AiLGNS，买到多少转给他多少，然后告诉网站记为已发。
     这样不管币价怎么动，钱包都不会不够发（每个人花的就是自己那份 DAI）。

钥匙：WEEKLY_KEY = 周榜专用钱包的私钥（这个钱包只放周榜的钱；站长自己建、自己放进 GitHub Secret）。
      BACKUP_KEY = 读写网站发奖记录用的口令。
没有 WEEKLY_KEY：只做 ①，不发钱。这个脚本碰不到别的钱包，也不改任何合约。

绝不重复发（三道）：
  1. 动钱之前先到网站把这一笔「锁住」（action=lock）。锁不上（已发 / 已锁 / 没烧）就跳过。
  2. 锁上以后才买币、转账；转完马上把交易哈希写进 data/weekly_prize.json，再告诉网站记为已发。
  3. 网站记账没成：下一轮只补记账，不再发。锁了以后一枚币都没买到（卖单被别人抢了）：钱没动，解锁下轮再来。
     买到了但没转成（中途断了）：这一笔就一直锁着，网站会提醒站长人工核对，脚本不会自己重发。
"""
import argparse
import json
import os
import time
import urllib.request

from eth_account import Account

from chains import rpc, send_tx

SITE = "https://ailgns.dev/api/weekly"
BOOK = "https://ailgns.dev/api/book?m=ailgns&fresh=1"
DAI = "0x83fd06f0846d9d90b3016bf670efe2e0b11cde14"  # Anubis 的 DAI：原生币的 ERC20 外壳
AIL = "0x256edb652db43e721ec6a12b6b51b6967d3bb8cb"
AIL_PAIR = "0x0a1cce65d995c6c5100e6f5ba3c388ef34cecaf0"  # token0 = AiLGNS，token1 = DAI
OTC = "0x4e730be6d46be71223ded5a32dc45799f6d4bc15"  # AiLGNS/DAI OTC 合约
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "weekly_prize.json")
TX = "https://anubisscan.io/tx/"
GAS_KEEP = 5 * 10**17  # 发完以后钱包里至少还剩 0.5 DAI 付 Gas
BUY_CAP = 1.5  # 买入限价：单价不高于 DEX 现价 × 1.5（和每日奖池、网站预言机封顶一致）
COVER = 0.95  # 盘口上限价内的卖单至少够花掉这笔奖金的 95% 才动手，不然等下一轮（免得只买到一点点就发）
ONE = 10**18
MAX_U = (1 << 256) - 1


def pad(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def http(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, headers={"content-type": "application/json", "user-agent": "ailgns-keeper"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def num(to, data):
    return int(rpc("anubis", "eth_call", [{"to": to, "data": data}, "latest"]), 16)


def wei(amt):
    return int(round(float(amt) * 100)) * 10**16  # 网站给的是两位小数的 DAI


def sells_under_cap(now_ts):
    """本站 OTC 上限价以内、还开着的 AiLGNS 卖单，从便宜到贵：[(单价, 单号, give_t, want_t, give_l)]"""
    w = rpc("anubis", "eth_call", [{"to": AIL_PAIR, "data": "0x0902f1ac"}, "latest"])[2:]
    r_ail, r_dai = int(w[:64], 16), int(w[64:128], 16)
    cap = r_dai * BUY_CAP / r_ail
    bk = http(BOOK)
    ws, frm = bk["orders"][2:], int(bk.get("from", 0))
    ws = [ws[k:k + 64] for k in range(0, len(ws), 64)]
    n_ord = int(ws[1], 16) if len(ws) > 1 else 0
    out = []
    for k in range(n_ord):
        o = ws[2 + k * 7: 9 + k * 7]
        if len(o) < 7:
            break
        sell, is_open, expiry = int(o[1], 16) == 1, int(o[2], 16) == 1, int(o[3], 16)
        give_t, want_t, give_l = int(o[4], 16), int(o[5], 16), int(o[6], 16)
        if sell and is_open and give_l > 0 and expiry > now_ts + 60 and give_t > 0 and want_t / give_t <= cap:
            out.append((want_t / give_t, frm + k, give_t, want_t, give_l))
    out.sort()
    return out, cap


def plan(sells, budget):
    """用 budget（DAI）从便宜到贵吃单：[(单号, 买多少枚, 付多少 DAI, 单价)]。每张单至少付 1 DAI（合约的最小吃单额）"""
    fills, spent = [], 0
    for px, oid, give_t, want_t, give_l in sells:
        if budget - spent < ONE:
            break
        amount = min(give_l, (budget - spent) * give_t // want_t)
        pay = -(-amount * want_t // give_t)  # 向上取整
        while pay > budget - spent and amount > 0:
            amount -= 10**12
            pay = -(-amount * want_t // give_t)
        if pay < ONE or amount <= 0:
            continue
        fills.append((oid, amount, pay, px))
        spent += pay
    return fills, spent


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

    def mark_paid(pid, rec):
        http(SITE, {"action": "paid", "id": pid, "tx": rec["tx"], "ail": rec["ail"], "key": key})
        st.setdefault("ok", {})[pid] = rec  # 网站记上了：挪到「办完的」里，下轮不用再报
        st["sent"].pop(pid, None)
        save()

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
    # 上一轮发出去了、但网站没记上的：只补记账
    for pid, rec in list(st["sent"].items()):
        if rec and rec.get("tx") and not dry:
            try:
                mark_paid(pid, rec)
            except Exception as e:
                print(f"⚠️ 补记账没成（下轮再试）：{pid} {e}")
    todo = r.get("pay") or []
    pool = (r.get("pool") or "").lower()
    if not todo:
        print("周榜：现在没有要发的奖")
        return
    acct = Account.from_key(os.environ["WEEKLY_KEY"]) if os.environ.get("WEEKLY_KEY") else None
    if not acct and not dry:
        print(f"周榜：有 {len(todo)} 笔可以发，但没有 WEEKLY_KEY（周榜钱包还没配好），这一轮不发")
        return
    me = acct.address.lower() if acct else pool
    if not pool or me != pool:
        print(f"⚠️ WEEKLY_KEY 对应的钱包 {me} 和网站登记的周榜钱包 {pool or '（没填）'} 不是同一个，不发")
        return
    ail_bal = lambda: num(AIL, "0x70a08231" + pad(me))
    for p in todo:
        pid, to, budget = p["id"], p["a"].lower(), wei(p["amt"])
        if pid in st["sent"] or pid in st.get("ok", {}):
            print(f"{pid} 本机记着已经锁过 / 发过，跳过（锁了没发成的等人工核对）")
            continue
        bal = int(rpc("anubis", "eth_getBalance", [me, "latest"]), 16)
        if budget < ONE or bal < budget + GAS_KEEP:
            print(f"⚠️ 周榜钱包有 {bal / 1e18:.4f} DAI，不够拿 {p['amt']} DAI 给 {to} 买币，这笔先不发")
            continue
        sells, cap = sells_under_cap(int(time.time()))
        fills, spend = plan(sells, budget)
        if spend < budget * COVER:
            print(f"OTC 上 ≤ {cap:.3f} DAI/枚 的卖单只够花 {spend / 1e18:.2f} DAI，不到 {p['amt']} DAI 的 95%，{pid} 这轮先不发")
            continue
        if dry:
            print(f"（dry-run）会用 {spend / 1e18:.2f} DAI 吃 {len(fills)} 张卖单，买约 {sum(f[1] for f in fills) * 99 / 100 / 1e18:.6f} AiLGNS 转给 {to}（{pid}）")
            continue
        lk = http(SITE, {"action": "lock", "id": pid, "key": key})
        if not lk.get("ok") or str(lk.get("a", "")).lower() != to or wei(lk.get("amt", 0)) != budget:
            print(f"{pid} 网站没给锁（{lk}），跳过")
            continue
        st["sent"][pid] = {}  # 锁上了、正要买币
        save()
        before = ail_bal()
        if num(DAI, "0xdd62ed3e" + pad(me) + pad(OTC)) < budget:
            send_tx("anubis", acct, DAI, "0x095ea7b3" + pad(OTC) + f"{MAX_U:064x}", "周榜钱包授权 OTC 用 DAI")
        for oid, amount, pay, px in fills:
            try:
                h = send_tx("anubis", acct, OTC, "0x6fe4a448" + f"{oid:064x}" + f"{amount:064x}" + f"{pay:064x}", f"周榜买入 AiLGNS #{oid}")  # fill(id, amount, pay)
                print(f"吃单 #{oid}：花 {pay / 1e18:.2f} DAI（{px:.3f} DAI/枚） {TX}{h}")
            except Exception as e:  # 被人抢先吃了等情况：跳过这张
                print(f"吃单 #{oid} 失败，跳过：{str(e)[:120]}")
        got = ail_bal() - before  # 实际到手多少就发多少（吃单手续费 1% 已经扣在里面）
        if got <= 0:
            st["sent"].pop(pid, None)  # 一枚都没买到：钱没动，解锁，下轮再来
            save()
            try:
                http(SITE, {"action": "unlock", "id": pid, "key": key})
            except Exception as e:
                print(f"⚠️ 解锁没成：{e}")
            print(f"{pid} 这轮一枚都没买到（卖单被抢了），已解锁，下轮再发")
            continue
        h = send_tx("anubis", acct, AIL, "0xa9059cbb" + pad(to) + f"{got:064x}", "周榜奖金")
        rec = {"tx": h, "ail": round(got / 1e18, 6)}
        st["sent"][pid] = rec
        save()  # 币一转出去马上存，后面出错也不会忘
        print(f"周榜奖金：{rec['ail']} AiLGNS（{p['amt']} DAI 买的）→ {to} {TX}{h}")
        try:
            mark_paid(pid, rec)
        except Exception as e:
            print(f"⚠️ 网站记账没成（下轮补记，不会重发）：{e}")


if __name__ == "__main__":
    main()
