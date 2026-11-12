"""🔮 AiLGNS 每日预言家：域名收入的 70% 留作奖金（进奖池钱包买 AiLGNS），30% 退回收款钱包；奖金只以 AiLGNS 发放。2026-11-07 之前的欠款一笔勾销。

钱怎么走（不改任何合约，奖池钱包和 keeper 都只以普通用户身份操作）：
  域名注册 / 续费的钱直接进回购分账合约 BUYBACK（0x99f4）。
  ① 每次运行先读域名合约的 Paid 事件，累计「应留奖金」= 域名收入（costDai）× 70%（站长自己付的不算）。
  ② BUYBACK 里有 DAI / AiLGNS 时，用合约自带的 refund() 退回收款钱包 SOURCE；「应留奖金」那部分立刻用
     SOURCE 给触发钱包的授权额度 transferFrom 到【奖池钱包 POOL】；其余照常回购分账。
  ③ 奖池钱包里闲置 DAI 满 100，就在本站 OTC 挂一张买单（买一上面一点点，≤ 卖一、≤ DEX × 1.02）等人来卖；发奖急用币时才撤单吃卖单（限价 ≤ DEX × 1.5）。
  ④ 读 ailgns.dev/api/oracle?pay=1 的待发奖金，按网站算好的 AiLGNS 数量从奖池钱包直接 transfer 给中奖人；
     奖池 AiLGNS 不够就先买，还不够就这轮不发、下轮再试（永远不发 DAI）。
必须在 otc_buyback.py 之前运行（工作流里排在它前面）。
需要：OTC_KEEPER_KEY（触发钱包）、POOL_KEY（奖池钱包 0xd69f…dba4）、BACKUP_KEY（读写网站发奖记录）。
    python oracle_prize.py --dry-run   # 只算，不发交易
"""
import argparse
import json
import os
import time
import urllib.request

from eth_account import Account
from eth_utils import keccak

from chains import rpc, send_tx
from otc_buyback import orders as read_orders
from notify_mail import send as send_mail
from fast_oracle import FAST_START, numeric_regs

BUYBACK = "0x99f47ca8e7ed873e01a337aefc5b75f5328d2da3"
SOURCE = "0xedb9c6900a11d2dd37da6b59f3604c35a39db1e6"
DAI = "0x83fd06f0846d9d90b3016bf670efe2e0b11cde14"
AIL = "0x256edb652db43e721ec6a12b6b51b6967d3bb8cb"
LGNS = "0x4d1d808a081fdac440703b3765fc61f8028c06b8"  # 9 位小数；客户用 LGNS 付的域名费会停在回购合约里
AIL_PAIR = "0x0a1cce65d995c6c5100e6f5ba3c388ef34cecaf0"  # token0 = AiLGNS，token1 = DAI
NAMES = ["0xbc408e9d7350967e7247c917a086cb4ece0ed988", "0x93af23fe74e5180f54f0996fa6bb6c157775f7f3"]
PAID = "0x26c4308021cef5cdbc618fee8b61faf9de6b8f7e4e49fac43869dc1a26756408"  # Paid(payer, token, amount, costDai)
REG = "0xa15b66031269f1c32be8db1e4353108dc76c84574a7c6697527e431ad921462a"  # NameRegistered：data 里有 label（和 Paid 同一笔交易）
# ⚡ 极速预言家资金池（站长 2026-11-12 定稿）：上线区块（fast_oracle.FAST_START）之后【新注册的纯数字 .ailgns 域名】的收入，
#   10% 留运营收入、90% 进这个专用钱包，和每日奖池分开；其它域名收入（含纯数字域名的续费）照旧 70/30。
#   FAST_START 没填 = 还没上线，全部照旧。哪些算「纯数字新注册」用 fast_oracle.numeric_regs 判断，和发票是同一个函数。
FAST_POOL = "0x8610bf581b1c10cb7d372f93b8cdcde382a7c1d1"
FAST_BPS = 9000
OWN = {SOURCE, "0x89dd06754bb239b6c4c92bc47c8fa72889084e27"}  # 站长自己付的不算收入
START = 18161697  # 域名合约（第 3 版）部署区块
START_TS = 1793721600  # 2026-11-04 00:00 北京时间：从这一刻起的域名收入留 20% 奖金（11 月 4 日的 20% = 开局奖池，第一期 11 月 6 日开奖）
SHARE_BPS = 7000  # 70%（站长 2026-11-07 定稿：OTC 1% 和域名 30% 进收款钱包，域名 70% 进奖池买 AiLGNS；2026-11-07 之前的欠款一笔勾销，见 owed_cleared）
SITE = "https://ailgns.dev/api/oracle"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "oracle_prize.json")
BUY_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "buyback.json")
TX = "https://anubisscan.io/tx/"
POOL_ADDR = "0xd69fd045e23c8a45c6b3f6b9a4ef66592d51dba4"  # 奖池钱包（只放奖池的钱，私钥在 GitHub Secret POOL_KEY）
OTC = "0x4e730be6d46be71223ded5a32dc45799f6d4bc15"  # AiLGNS/DAI OTC 合约
BOOK = "https://ailgns.dev/api/book?m=ailgns&fresh=1"
REFUND_MIN = 10 * 10**18  # 回购合约里攒到 10 DAI 才退一次，省 Gas
LGNS_REFUND_MIN = 100 * 10**9  # LGNS 攒到 100 个才退一次
BUY_MIN = 100 * 10**18  # 奖池 DAI 满 100 就去 OTC 买 AiLGNS
BUY_CAP = 1.5  # 买入限价：单价不高于 DEX 现价 × 1.5（和网站预言机封顶一致）
# 🪧 站长 2026-11-07 定：奖池换币不再吃卖单，改成在 OTC 挂买单（挂在买一上面一点点），等人来卖：不付 1% 手续费、不吃别人的价，盘口还多一层真实买方深度。
#    只挂一张，奖池所有闲置 DAI 都挂上（留 2 DAI 当 Gas）；价格偏离目标 1% 以上或 1 天内到期就撤了重挂；发奖急需币时先撤单再吃单。
BID_TTL = 7 * 86400
BID_DRIFT = 0.01
BID_MIN = BUY_MIN  # 挂单起步 100 DAI
MAX_U = (1 << 256) - 1


def sel(sig):
    return "0x" + keccak(text=sig).hex()[:8]


def pad(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def num(to, data):
    return int(rpc("anubis", "eth_call", [{"to": to, "data": data}, "latest"]), 16)


def http(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, headers={"content-type": "application/json", "user-agent": "ailgns-keeper"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    dry = ap.parse_args().dry_run
    st = json.load(open(STATE)) if os.path.exists(STATE) else {"scanned_to": START - 1, "income": "0", "owed": "0", "held": "0", "held_ail": "0", "paid": []}
    latest = int(rpc("anubis", "eth_blockNumber", []), 16)
    if not st.get("start_ok") and int(rpc("anubis", "eth_getBlockByNumber", [hex(latest), False])["timestamp"], 16) < START_TS:
        st["scanned_to"] = latest  # 还没到开始时间：之前的收入都不算，直接跳过

    # ① 新的域名收入 → 应留奖金
    new_cost = 0
    fast_cost = 0  # 极速预言家的票款（上线后新注册的纯数字 .ailgns 域名），单独算
    for b in range(st["scanned_to"] + 1, latest + 1, 5000):
        end = min(latest, b + 4999)
        for c in NAMES:
            fast_on = bool(FAST_START) and c == NAMES[0] and end >= FAST_START
            ls = rpc("anubis", "eth_getLogs", [{"address": c, "fromBlock": hex(b), "toBlock": hex(end), "topics": [[PAID, REG]] if fast_on else [PAID]}])
            fast_paid = {(r[5], r[6]) for r in numeric_regs(ls) if r[0] >= FAST_START} if fast_on else set()  # (交易, Paid 日志序号)
            for l in ls:
                if l["topics"][0] != PAID:
                    continue
                if "0x" + l["topics"][1][26:] in OWN:
                    continue
                if not st.get("start_ok"):  # 开始时间前的收入不算
                    bt = int(rpc("anubis", "eth_getBlockByNumber", [l["blockNumber"], False])["timestamp"], 16)
                    if bt < START_TS:
                        continue
                cost = int(l["data"][2 + 64:2 + 128], 16)
                if (l["transactionHash"], int(l["logIndex"], 16)) in fast_paid:
                    fast_cost += cost
                else:
                    new_cost += cost
        st["scanned_to"] = end
        if not st.get("start_ok") and int(rpc("anubis", "eth_getBlockByNumber", [hex(end), False])["timestamp"], 16) >= START_TS:
            st["start_ok"] = True
    st["income"] = str(int(st["income"]) + new_cost)
    st["fast_income"] = str(int(st.get("fast_income", "0")) + fast_cost)
    st["fast_owed"] = str(int(st.get("fast_owed", "0")) + fast_cost * FAST_BPS // 10000)  # 欠极速资金池的（90%）
    if fast_cost:
        print(f"极速预言家票款 {fast_cost / 1e18:.2f} DAI → 90% 应转极速资金池 {fast_cost * FAST_BPS / 10000 / 1e18:.2f}")
    owed = int(st["owed"]) + new_cost * SHARE_BPS // 10000
    if not st.get("owed_cleared"):  # 站长 2026-11-07：之前欠奖池的一笔勾销，只留从现在起每笔域名收入的 70%
        owed = new_cost * SHARE_BPS // 10000
        st["to_pool_dai"] = st["to_pool_ail"] = "0"
        st["owed_cleared"] = True
    held, held_ail = int(st["held"]), int(st.get("held_ail", "0"))
    w = rpc("anubis", "eth_call", [{"to": AIL_PAIR, "data": "0x0902f1ac"}, "latest"])[2:]
    r_ail, r_dai = int(w[:64], 16), int(w[64:128], 16)  # AiLGNS 现价 = r_dai / r_ail
    print(f"新域名收入 {new_cost / 1e18:.2f} DAI；应留奖金 {owed / 1e18:.2f}，已留 {held / 1e18:.2f} DAI + {held_ail / 1e18:.6f} AiLGNS")

    def save():
        st["owed"], st["held"], st["held_ail"] = str(owed), str(held), str(held_ail)
        with open(STATE, "w") as f:
            json.dump(st, f)

    keeper = Account.from_key(os.environ["OTC_KEEPER_KEY"]) if os.environ.get("OTC_KEEPER_KEY") else None
    pool = Account.from_key(os.environ["POOL_KEY"]) if os.environ.get("POOL_KEY") else None
    pool_addr = pool.address.lower() if pool else POOL_ADDR
    bal = lambda tok, who: num(tok, "0x70a08231" + pad(who))
    now_ts = int(time.time())

    # 欠奖池但还没转过去的（授权不够等原因），每次先补转
    def move_to_pool(tok, amount, why):
        key = "to_pool_dai" if tok == DAI else "to_pool_ail"
        if amount <= 0 or not keeper or dry:
            return
        allow = num(tok, "0xdd62ed3e" + pad(SOURCE) + pad(keeper.address))
        have = bal(tok, SOURCE)
        n = min(amount, allow, have)
        if n <= 0:
            st[key] = str(int(st.get(key, "0")) + amount)
            print(f"⚠️ 欠奖池 {amount / 1e18:.6f} {'DAI' if tok == DAI else 'AiLGNS'} 没转成（授权 {allow / 1e18:.4f}，余额 {have / 1e18:.4f}），记账下次再转")
            return
        h = send_tx("anubis", keeper, tok, "0x23b872dd" + pad(SOURCE) + pad(pool_addr) + f"{n:064x}", "奖金转入奖池钱包")
        st[key] = str(int(st.get(key, "0")) + amount - n)
        print(f"{why}：{n / 1e18:.6f} {'DAI' if tok == DAI else 'AiLGNS'} 转入奖池钱包 {TX}{h}")
    # ⚡ 欠极速资金池的 DAI：从谷歌社交账号钱包用周转授权转到 FAST_POOL（专用钱包，和每日奖池分开）
    fast_owe = int(st.get("fast_owed", "0"))
    if fast_owe > 0 and keeper and not dry:
        allow = num(DAI, "0xdd62ed3e" + pad(SOURCE) + pad(keeper.address)); have = bal(DAI, SOURCE)
        n = min(fast_owe, allow, have)
        if n > 10**17:
            h = send_tx("anubis", keeper, DAI, "0x23b872dd" + pad(SOURCE) + pad(FAST_POOL) + f"{n:064x}", "极速预言家票款 90% 转入极速资金池")
            st["fast_owed"] = str(fast_owe - n)
            print(f"极速资金池：{n / 1e18:.2f} DAI 已转入 {FAST_POOL} {TX}{h}")
        else:
            print(f"⚠️ 欠极速资金池 {fast_owe / 1e18:.2f} DAI 没转成（授权 {allow / 1e18:.2f}，余额 {have / 1e18:.2f}），下轮再转")
    for tok, key in ((DAI, "to_pool_dai"), (AIL, "to_pool_ail")):
        owe_pool = int(st.get(key, "0"))
        if owe_pool > 0:
            st[key] = "0"
            move_to_pool(tok, owe_pool, "补转欠款")

    # ② 从 BUYBACK 里留出奖金：refund 回 SOURCE，应留的那份马上转进奖池钱包；多退的记回 otc_buyback 的待处理收入
    for tok, name in ((DAI, "DAI"), (AIL, "AiLGNS")):
        carry = bal(tok, BUYBACK)
        if carry <= 0 or not keeper or dry:
            continue
        if carry < REFUND_MIN and tok == DAI:
            print(f"回购合约里只有 {carry / 1e18:.4f} {name}，不到 {REFUND_MIN / 1e18:.0f} 先不退（省 Gas）")
            continue
        want = owed if tok == DAI else owed * r_ail // r_dai  # AiLGNS 按现价折算
        keep = min(carry, want)
        h = send_tx("anubis", keeper, BUYBACK, sel("refund(address)") + pad(tok), f"退回 {name} 到收款钱包")
        # 退回收款钱包的 30% 不再记成「待回购收入」（站长 2026-11-07：这部分留在收款钱包；以后恢复分账也只从恢复那天起算）
        owed = owed - keep if tok == DAI else max(0, owed - keep * r_dai // r_ail)
        save()
        print(f"退回 {carry / 1e18:.6f} {name} 到收款钱包：留 {keep / 1e18:.6f} 当奖金 {TX}{h}")
        move_to_pool(tok, keep, "留出的奖金")

    # ②b LGNS 域名费：整笔退回收款钱包，不留奖金、不换币（站长 2026-11-10：自动退回钱包）
    try:
        lg = bal(LGNS, BUYBACK)
        if lg >= LGNS_REFUND_MIN:
            if keeper and not dry:
                h = send_tx("anubis", keeper, BUYBACK, sel("refund(address)") + pad(LGNS), "退回 LGNS 到收款钱包")
                print(f"退回 {lg / 1e9:.2f} LGNS 到收款钱包 {TX}{h}")
            else:
                print(f"回购合约里有 {lg / 1e9:.2f} LGNS，正式运行时会整笔退回收款钱包")
    except Exception as e:
        print(f"⚠️ 退回 LGNS 没成：{e}")

    # ③ 奖池 DAI 满 100 → 在本站 OTC 吃最便宜的卖单买 AiLGNS（限价 ≤ DEX × 1.5）
    def buy_ailgns(budget, why):
        if not pool or dry or budget < 10**18:
            return 0, 0
        cap = r_dai * BUY_CAP / r_ail  # 每枚最多付多少 DAI（两种币都是 18 位小数）
        bk = http(BOOK)
        w, frm = bk["orders"][2:], int(bk.get("from", 0))
        ws = [w[k:k + 64] for k in range(0, len(w), 64)]
        n_ord = int(ws[1], 16) if len(ws) > 1 else 0
        sells = []
        for k in range(n_ord):
            o = ws[2 + k * 7: 9 + k * 7]
            if len(o) < 7:
                break
            sell, is_open, expiry = int(o[1], 16) == 1, int(o[2], 16) == 1, int(o[3], 16)
            give_t, want_t, give_l = int(o[4], 16), int(o[5], 16), int(o[6], 16)
            if not (sell and is_open and give_l > 0 and expiry > now_ts and give_t > 0):
                continue
            px = want_t / give_t
            if px <= cap:
                sells.append((px, frm + k, give_t, want_t, give_l))
        sells.sort()
        if not sells:
            print(f"OTC 没有 ≤ {cap:.3f} DAI/枚 的卖单，先不买")
            return 0, 0
        if num(DAI, "0xdd62ed3e" + pad(pool_addr) + pad(OTC)) < budget:
            send_tx("anubis", pool, DAI, "0x095ea7b3" + pad(OTC) + f"{MAX_U:064x}", "奖池钱包授权 OTC 用 DAI")
        spent = got = 0
        for px, oid, give_t, want_t, give_l in sells:
            if budget - spent < 10**18:
                break
            amount = min(give_l, (budget - spent) * give_t // want_t)
            pay = -(-amount * want_t // give_t)  # 向上取整
            while pay > budget - spent and amount > 0:
                amount -= 10**12
                pay = -(-amount * want_t // give_t)
            if pay < 10**18 or amount <= 0:
                continue
            data = "0x6fe4a448" + f"{oid:064x}" + f"{amount:064x}" + f"{pay:064x}"  # fill(id, amount, pay)
            try:
                h = send_tx("anubis", pool, OTC, data, f"奖池买入 AiLGNS #{oid}")
            except Exception as e:  # 被人抢先吃了等情况：跳过这张
                print(f"吃单 #{oid} 失败，跳过：{str(e)[:120]}")
                continue
            spent += pay
            got += amount * 99 // 100  # 吃单方手续费 1%，从收到的币里扣
            print(f"{why}：吃单 #{oid} 花 {pay / 1e18:.2f} DAI 买到约 {amount * 99 / 100 / 1e18:.6f} AiLGNS（{px:.3f} DAI/枚） {TX}{h}")
        return spent, got
    # 奖池自己挂在 OTC 的买单（maker = 奖池钱包、买币、还开着）
    def pool_bids():
        return [o for o in read_orders(OTC) if o[1] == pool_addr and not o[2] and o[3] and o[7] > 0]

    def cancel_bids(why):
        for o in pool_bids():
            if dry:
                print(f"（dry-run）会撤买单 #{o[0]}：{why}")
                continue
            h = send_tx("anubis", pool, OTC, "0x40e58ee5" + f"{o[0]:064x}", f"奖池撤买单 #{o[0]}")
            print(f"撤买单 #{o[0]}（{why}），{o[7] / 1e18:.2f} DAI 退回奖池 {TX}{h}")

    def maintain_bid():
        """奖池换币：维持一张挂在买一上面一点点的买单（≤ 卖一、≤ DEX × 1.02、≤ DEX × BUY_CAP）"""
        book = [o for o in read_orders(OTC) if o[3] and o[4] > now_ts + 60 and o[7] > 0 and o[1] != pool_addr]
        bids = [o[5] / o[6] for o in book if not o[2]]
        asks = [o[6] / o[5] for o in book if o[2]]
        dex = r_dai / r_ail
        target = max(bids) * 1.001 if bids else dex * 0.98
        target = min(target, dex * 1.02, dex * BUY_CAP, (min(asks) * 0.999) if asks else target)
        mine = pool_bids()
        free = bal(DAI, pool_addr) - 2 * 10**18
        stale = [o for o in mine if abs(o[5] / o[6] - target) > target * BID_DRIFT or o[4] < now_ts + 86400]
        if len(mine) > 1 or stale or (mine and free >= BID_MIN):
            cancel_bids("价格偏了 / 快到期 / 有新 DAI 要并进来" if mine else "")
            free = (bal(DAI, pool_addr) - 2 * 10**18) if not dry else free + sum(o[7] for o in mine)
            mine = []
        if mine:
            o = mine[0]
            print(f"奖池买单 #{o[0]} 在挂：{o[5] / o[6]:.3f} DAI/枚，剩 {o[7] / 1e18:.2f} DAI（目标 {target:.3f}，买一 {max(bids) if bids else 0:.3f} / 卖一 {min(asks) if asks else 0:.3f} / DEX {dex:.3f}）")
            return
        if free < BID_MIN:
            print(f"奖池闲置 DAI {max(free, 0) / 1e18:.2f}，不到 {BID_MIN / 1e18:.0f}，先不挂")
            return
        want = free * 10**18 // int(target * 1e18)
        if dry:
            print(f"（dry-run）会挂买单：{free / 1e18:.2f} DAI 买 {want / 1e18:.6f} AiLGNS（{target:.3f} DAI/枚；买一 {max(bids) if bids else 0:.3f} / 卖一 {min(asks) if asks else 0:.3f} / DEX {dex:.3f}）")
            return
        if num(DAI, "0xdd62ed3e" + pad(pool_addr) + pad(OTC)) < free:
            send_tx("anubis", pool, DAI, "0x095ea7b3" + pad(OTC) + f"{MAX_U:064x}", "奖池钱包授权 OTC 用 DAI")
        h = send_tx("anubis", pool, OTC, "0x9128a740" + f"{0:064x}" + f"{free:064x}" + f"{want:064x}" + f"{BID_TTL:064x}", "奖池挂买单")
        st["bid"] = {"t": now_ts, "dai": round(free / 1e18, 2), "px": round(target, 3), "tx": h}
        print(f"奖池挂买单：{free / 1e18:.2f} DAI 买 {want / 1e18:.6f} AiLGNS（{target:.3f} DAI/枚，7 天） {TX}{h}")

    # ④ 发奖：只发 AiLGNS，按网站算好的数量，从奖池钱包直接转给中奖人
    key = os.environ.get("BACKUP_KEY")
    sent, short = [], []
    px_otc = 0.0
    try:
        px_otc = float(((http(SITE).get("funds") or {}).get("otc") or {}).get("px") or 0)
    except Exception:
        pass
    if not px_otc:
        px_otc = r_dai / r_ail
    if key:
        todo = http(SITE + "?pay=1&key=" + key).get("pay", [])
        for p in todo:
            ail = float(p.get("ail") or 0) or p["amt"] / px_otc
            n = int(round(ail * 10**6)) * 10**12
            if not pool or dry:
                short.append(f"{p['name']} {ail:.6f} AiLGNS（dry-run）")
                continue
            have = bal(AIL, pool_addr)
            if have < n:  # 不够就先买：钱挂在买单里的话先撤回来
                need_dai = int((n - have) * px_otc * 1.03)
                if bal(DAI, pool_addr) - 2 * 10**18 < need_dai:
                    cancel_bids("发奖要用钱")
                buy_ailgns(min(need_dai, bal(DAI, pool_addr) - 2 * 10**18), "发奖补币")
                have = bal(AIL, pool_addr)
            if have < n:
                short.append(f"{p['name']} {ail:.6f} AiLGNS（奖池只有 {have / 1e18:.6f}，下轮再发）")
                continue
            h = send_tx("anubis", pool, AIL, "0xa9059cbb" + pad(p["a"]) + f"{n:064x}", "发预言家奖金")
            http(SITE, {"action": "paid", "id": p["id"], "tx": h, "key": key, "ail": round(n / 1e18, 6)})
            st["paid"] = (st.get("paid") or [])[-200:] + [{"id": p["id"], "amt": p["amt"], "ail": round(n / 1e18, 6), "tx": h}]
            sent.append(f"{p['day']} { {'jackpot': '🔮 神算子', 'closest': '👁 天眼通', 'second': '🌿 地灵通', 'king': '💰 贡献王'}.get(p['kind'], p['kind']) } {p['name']}：{n / 1e18:.6f} AiLGNS（≈ {p['amt']} DAI） {TX}{h}")
    # ⑤ 发完奖，剩下的闲置 DAI 挂成买单（奖池换币）
    if pool:
        try:
            maintain_bid()
        except Exception as e:
            print(f"奖池挂买单出错（下轮再试）：{str(e)[:160]}")
    # 记账：奖池钱包链上实际余额（网站也直接读链，这里只是留个记录）
    held, held_ail = bal(DAI, pool_addr), bal(AIL, pool_addr)
    if not dry:
        save()
    print(f"奖池钱包 {pool_addr}：{held / 1e18:.2f} DAI + {held_ail / 1e18:.6f} AiLGNS")
    if sent:
        print("\n".join(sent))
        ops = [e.strip() for e in (os.environ.get("OPS_EMAILS") or "").split(",") if e.strip()]
        send_mail("AiLGNS 预言家奖金已发（AiLGNS）", "\n".join(sent) + "\n\n—— AiLGNS 服务站", ops or None)
    if short:
        print("还没发：" + "；".join(short))


if __name__ == "__main__":
    main()
