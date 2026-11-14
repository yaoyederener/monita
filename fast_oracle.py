"""⚡ 极速预言家（站长 2026-11-12 定稿）：算账 + 结算 + 发奖。

规则：
  一期 10 分钟，整点对齐（:00、:10、:20…）。期号 = 区块时间戳 // 600。
  开奖 = 本期结束后第一个区块的哈希 % 10（0 到 9）。
  票   = 上线之后新注册的【纯数字 .ailgns 域名】，付了多少 DAI 就是多少票（1 DAI = 1 票，按付款钱包记）。续费不算票。
  下注 = 持票钱包给极速资金池转 0 个 DAI（只花 Gas），交易数据末尾带「ailgns-fast:数字:票数」。
         这样下注会留下一条 Transfer 日志，机器人按日志就能找全，不靠网站登记，谁也漏不掉、改不了。
         票数超过剩余的，按剩余算；一张不剩的作废。下注算进交易所在区块的那一期。
  分钱 = 每张下注的票带 0.9 DAI 进本期奖池（另 0.1 归平台），猜中的按票数平分；没人中，整池滚到下一期。
  发奖 = 只发 AiLGNS：中奖的 DAI 按本站 OTC 预言机价折成 AiLGNS（卖单比预言机价还贵时按实际买入价），
         极速资金池钱包在本站 OTC 吃卖单买币，再转给中奖人。转账数据末尾带「ailgns-fast-pay:期号」，
         发没发过只认链上这条记录，状态文件丢了也不会重发。
钱怎么来：oracle_prize.py 把纯数字域名收入的 90% 从收款钱包转进极速资金池（和每日奖池分开）。
极速资金池钱包只做三件事：授权 OTC 用它的 DAI、在 OTC 吃卖单、把 AiLGNS 转给中奖人。不碰任何合约的管理权限。

    python fast_oracle.py --run              # 正式：记账、结算、发奖（要 FAST_POOL_KEY）
    python fast_oracle.py --run --dry-run    # 只算，不发交易、不存档
    python fast_oracle.py --days 3           # 假设 3 天前上线，用真实链上数据算一遍（只读）
    python fast_oracle.py --selftest         # 用假下注 + 真区块哈希验算分钱（只读）
"""
import argparse
import json
import os
import re
import time

from chains import rpc

FAST_POOL = "0x8610bf581b1c10cb7d372f93b8cdcde382a7c1d1"
DAI = "0x83fd06f0846d9d90b3016bf670efe2e0b11cde14"
AIL = "0x256edb652db43e721ec6a12b6b51b6967d3bb8cb"
AIL_PAIR = "0x0a1cce65d995c6c5100e6f5ba3c388ef34cecaf0"  # token0 = AiLGNS，token1 = DAI
OTC = "0x4e730be6d46be71223ded5a32dc45799f6d4bc15"  # AiLGNS/DAI OTC 合约
NAMES_AIL = "0xbc408e9d7350967e7247c917a086cb4ece0ed988"  # 只认 .ailgns
PAID = "0x26c4308021cef5cdbc618fee8b61faf9de6b8f7e4e49fac43869dc1a26756408"  # Paid(payer, token, amount, costDai)
REG = "0xa15b66031269f1c32be8db1e4353108dc76c84574a7c6697527e431ad921462a"  # NameRegistered(id, label, owner, cost, expires)
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
OWN = {"0xedb9c6900a11d2dd37da6b59f3604c35a39db1e6", "0x89dd06754bb239b6c4c92bc47c8fa72889084e27"}  # 站长自己付的不算票
FAST_START = 18976231  # 上线区块（2026-11-12 开闸）。之前注册的纯数字域名一律不算票（网站 lib/fast.js 里要填同一个数）
# 🆕 新规则（站长 2026-11-14 定稿）。V2_START = 新规则生效的区块；None = 还没开闸，一切照旧。开闸时和网站 lib/fast.js 填同一个数。
#   ① 票：不再只认纯数字域名。任何新注册的 .ailgns 域名都行，但要「批量」——同一个钱包 600 个区块（约 10 分钟）内注册 2 个及以上，每个都算票（付多少 DAI 多少票）。
#      链上看不出客户点没点批量按钮，所以用「10 分钟内买了 2 个以上」来认，机器人和网站各自从链上数，数出来一样。落单的一个不算票，收入照旧进每日奖池。
#   ② 返奖（站长 2026-11-14 改口：不是只返 90%）：猜中每张票发 1.9 DAI = 本金 1 + 净赚 0.9，不分别人的钱；没猜中一分不返。
#      每张票只有 0.9 DAI 进了极速池，所以猜中的人多拿的那 1 DAI 是从没猜中的票钱里出的。运营收入 = 这一期下注的新票 × 0.9 − 猜中的新票 × 1.9，可能是负数（那一期赔出去的比收进来的多），累计起来攒够 OPS_MIN 才转回收款钱包。
#      长期看十个数字猜中一个，每张票平均赔 0.19、留 0.71；但有人重仓一个数字猜中时，极速池要先垫钱。池里的钱不够发时不打折，等钱够了再发并发邮件报警。
#   ③ 老票：V2_START 之前买的（纯数字域名）按旧规则用完——同一期的老票下注进老奖池，猜中的按票数分，没人中滚存。一个钱包两种票都有时先用老票。
V2_START = 19100010  # 2026-11-14 约 13:47 北京时间开闸（站长「开闸，上限 100 张」）
PAIR_BLOCKS = 600
OPS_PREFIX = "ailgns-fast-ops"
OPS_MIN = 20 * 10**18
WIN2_PER_TICKET = 19 * 10**17  # 新票猜中每张发 1.9 DAI
CAP2 = 100  # 单注上限（站长 2026-11-14 定）：每个钱包每期最多押 100 张新票（所有数字加起来）。超出的部分不算下注，票还留在钱包里。防有人重仓押中把极速池掏空：最坏一个钱包一期垫 100 DAI。老票不限（老规则是大家分奖池，不用池子垫钱）。网站 lib/fast.js 的 CAP2 要填同一个数
SOURCE = "0xedb9c6900a11d2dd37da6b59f3604c35a39db1e6"  # 收款钱包（谷歌社交账号钱包）
# 🤖 代注册机器人钱包（网站 names-batch.js 里的 REG）：客户授权 DAI 给它，它替客户注册，域名直接归客户。链上看「付款的人」是它，不是客户。
#   老规则：页面写明「交给机器人代点的不算票」→ 它付款的注册一律不算老票。
#     （2026-11-14 查出来：之前按付款钱包记票，把它替大奖得主注册的 278 个纯数字域名算成了 1,390 张票，极速池多收了约 674 DAI。站长同意改对并重算。）
#   新规则：批量注册可以交给它代点，票记给域名的主人（下单的客户）。
REG_BOT = "0x21425f852a1e0fb798fb9ebeb288def27bc0f2d1"
STATE_V = 2  # 账本版本：记票的口径变了就加 1，下一轮自动从上线区块重新数一遍
ROUND = 600
TICKET_WEI = 10**18
POT_PER_TICKET = 9 * 10**17  # 每张票 0.9 DAI 进奖池
PREFIX = "ailgns-fast:"
PAY_PREFIX = "ailgns-fast-pay:"
CONFIRM = 20  # 只认 20 个块之前的数据，防链重组
BET_RE = re.compile(r"^([0-9]{1,10}):([1-9][0-9]{0,8})$")
MULTI_START = 19103400  # 一笔下注押多个数字，从这个区块起才认（约 2026-11-14 14:44 北京时间；网站 lib/fast.js 的 MULTI_START 填同一个数）


# 💰 奖池跟着买票涨（站长 2026-11-14 定）：从这个区块起，批量注册的票款每一笔都转 90% 进极速奖池，不再和之前多转进去的钱对冲（那笔留在池里当储备）。
#    oracle_prize.py 按它算「欠奖池多少」，网站 lib/fast.js 的 FLOW_START 填同一个数，用同一个口径算「在路上的票款」。
FLOW_START = 19105450  # 约 2026-11-14 15:18 北京时间


def decode_bet(memo, blk):
    """下注附言 → [(数字, 每个数字几张)]。老写法「7:3」= 数字 7 压 3 张；新写法「0123:1」= 0、1、2、3 各压 1 张。
    多个数字的写法只在 MULTI_START 之后认，数字不能重复；不合规返回 []。同一笔里的数字按写的顺序一个个记，票不够时排在后面的先没有。"""
    m = BET_RE.match(memo or "")
    if not m:
        return []
    ds = m.group(1)
    if len(ds) > 1 and (blk < MULTI_START or len(set(ds)) != len(ds)):
        return []
    return [(int(c), int(m.group(2))) for c in ds]
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fast_oracle.json")
SITE = "https://ailgns.dev/api/oracle"
TX = "https://anubisscan.io/tx/"
GAS_KEEP = 2 * 10**18  # 钱包里留 2 DAI 付 Gas（这条链的 Gas 就是 DAI）
BUY_CAP = 1.5  # 买入限价：单价不高于 DEX 现价 × 1.5（和网站预言机封顶一致）
BUY_SLACK = 1.02  # 买币预算最多比应发的 DAI 多 2%（再加 1 DAI 起步额）
MAX_U = (1 << 256) - 1


def pad(a):
    return "0x" + a.lower().replace("0x", "").rjust(64, "0")


def num(to, data):
    return int(rpc("anubis", "eth_call", [{"to": to, "data": data}, "latest"]), 16)


def bal_of(tok, who):
    return num(tok, "0x70a08231" + pad(who)[2:])


def logs(addr, a, b, topics):
    out = []
    for s in range(a, b + 1, 5000):
        out += rpc("anubis", "eth_getLogs", [{"address": addr, "fromBlock": hex(s), "toBlock": hex(min(b, s + 4999)), "topics": topics}])
    return out


def label_of(data):  # NameRegistered 的 data：ABI string
    try:
        h = data[2:]; off = int(h[:64], 16) // 32; ln = int(h[off * 64:off * 64 + 64], 16)
        return bytes.fromhex(h[(off + 1) * 64:(off + 1) * 64 + ln * 2]).decode("utf-8")
    except Exception:
        return ""


_blk = {}
def block(n):
    if n not in _blk:
        b = rpc("anubis", "eth_getBlockByNumber", [hex(n), False])
        _blk[n] = (int(b["timestamp"], 16), b["hash"]) if b else None
    return _blk[n]


def first_block_at(ts, latest):
    """时间戳 >= ts 的第一个区块号；还没出就返回 None。链大约 1 秒 1 块，先估再左右挪"""
    lt = block(latest)[0]
    if lt < ts:
        return None
    n = max(1, min(latest, latest - (lt - ts)))
    for _ in range(64):  # 估偏了就按差值跳
        t = block(n)[0]
        if t >= ts and block(n - 1)[0] < ts:
            return n
        n = max(1, min(latest, n + (ts - t if t < ts else -max(1, (t - ts) // 2 or 1))))
    lo, hi = 1, latest  # 兜底：二分
    while lo < hi:
        m = (lo + hi) // 2
        if block(m)[0] >= ts: hi = m
        else: lo = m + 1
    return lo


def all_regs(ls):
    """从域名合约的一批日志里挑出【新注册】：[(区块, 注册日志序号, 付款钱包, costDai, 域名, 交易, Paid 日志序号, 域名主人)]
    同一笔交易里 Paid 后面跟着 NameRegistered 才算（续费只有 Paid，不算）。站长自己付的不算。"""
    out, last_paid = [], {}
    ls = sorted(ls, key=lambda l: (int(l["blockNumber"], 16), int(l["logIndex"], 16)))
    for l in ls:
        tx = l["transactionHash"]
        if l["topics"][0] == PAID:
            last_paid[tx] = ("0x" + l["topics"][1][26:].lower(), int(l["data"][2 + 64:2 + 128], 16), int(l["logIndex"], 16))
            continue
        if l["topics"][0] != REG:
            continue
        paid = last_paid.pop(tx, None)
        lab = label_of(l["data"])
        if not paid or paid[0] in OWN:
            continue
        owner = ("0x" + l["topics"][2][26:].lower()) if len(l["topics"]) > 2 else paid[0]
        out.append((int(l["blockNumber"], 16), int(l["logIndex"], 16), paid[0], paid[1], lab, tx, paid[2], owner))
    return out


def numeric_regs(ls):
    """老规则的票：新注册的纯数字域名（格式同 all_regs）"""
    return [r for r in all_regs(ls) if re.fullmatch(r"[0-9]+", r[4])]


def pair_grants(regs):
    """新规则：一批注册里哪些算票、什么时候发票。regs = [(区块, 序号, 钱包, …)]（只传新规则生效以后的）。
    返回 {(区块, 序号): (发票区块, 发票序号, 先后)}，不在里面的 = 落单，不算票。
    规则：同一个钱包，前一个注册在 600 个区块以内 → 自己注册那一刻发票；
          前面没有、但后一个在 600 个区块以内（自己是这一批的第一个）→ 等第二个出现时一起发，排在第二个前面；
          前后都没有 → 落单。"""
    by, out = {}, {}
    for r in sorted(regs, key=lambda r: (r[0], r[1])):
        by.setdefault(r[2], []).append(r)
    for L in by.values():
        for k, r in enumerate(L):
            if k > 0 and r[0] - L[k - 1][0] <= PAIR_BLOCKS:
                out[(r[0], r[1])] = (r[0], r[1], 1)
            elif k + 1 < len(L) and L[k + 1][0] - r[0] <= PAIR_BLOCKS:
                out[(r[0], r[1])] = (L[k + 1][0], L[k + 1][1], 0)
    return out


def ticket_regs(ls):
    """哪些新注册算票：[(发票区块, 发票序号, 钱包, costDai, 域名, 交易, Paid 序号, 注册区块, 版本 1/2, 先后)]
    新规则那部分要看前后 600 个区块，所以传进来的日志要比自己要的范围往前多带 1200 个区块；调用的人只取发票区块（或注册区块）落在自己范围里的。"""
    regs, out = all_regs(ls), []
    who = lambda r: r[7] if r[2] == REG_BOT else r[2]  # 票记给谁：机器人代点的记给域名主人，自己点的记给付款钱包
    new = [(r[0], r[1], who(r)) for r in regs if V2_START and r[0] >= V2_START]
    g = pair_grants(new)
    for r in regs:
        if V2_START and r[0] >= V2_START:
            at = g.get((r[0], r[1]))
            if at:
                out.append((at[0], at[1], who(r), r[3], r[4], r[5], r[6], r[0], 2, at[2]))
        elif re.fullmatch(r"[0-9]+", r[4]) and r[2] != REG_BOT:  # 老规则：机器人代点的不算票
            out.append((r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[0], 1, 1))
    return out


def fast_paid_in(ls, a, b):
    """分钱用（oracle_prize.py）：注册区块落在 [a, b] 的票款 → {(交易, Paid 日志序号)}。
    新规则下要等后面 600 个区块出完才知道一个注册是不是落单，所以调用的人传的日志要到 b + 600、从 a − 1200 起。"""
    return {(r[5], r[6]) for r in ticket_regs(ls) if a <= r[7] <= b and r[7] >= (FAST_START or 0)}


def read_tickets(a, b):
    """[(发票区块, 发票序号, 钱包, 域名, 票数, 版本 1/2, 先后)]：发票区块落在 [a, b] 的"""
    lo = max(FAST_START or a, a - 2 * PAIR_BLOCKS) if V2_START else a  # 新规则要往前多看 1200 个区块
    return [(r[0], r[1], r[2], r[4], r[3] // TICKET_WEI, r[8], r[9]) for r in ticket_regs(logs(NAMES_AIL, lo, b, [[PAID, REG]])) if a <= r[0] <= b and r[3] >= TICKET_WEI]


def memo_of(tx, prefix):
    """交易是 transfer(…) 后面附了一段以 prefix 开头的字 → 返回后面的字，否则 None"""
    inp = (tx or {}).get("input", "")
    if not inp.startswith("0xa9059cbb") or len(inp) <= 138:
        return None
    try:
        txt = bytes.fromhex(inp[138:]).decode("utf-8")
    except Exception:
        return None
    return txt[len(prefix):] if txt.startswith(prefix) else None


def read_bets(a, b):
    """[(区块, 序号, 钱包, 数字, 票数, 交易, 这笔里的第几个数字)]：给极速池转 0 DAI，且交易是钱包自己直接发给 DAI 合约的"""
    out = []
    for l in logs(DAI, a, b, [TRANSFER, None, pad(FAST_POOL)]):
        if int(l["data"], 16) != 0:
            continue
        tx = rpc("anubis", "eth_getTransactionByHash", [l["transactionHash"]])
        frm = "0x" + l["topics"][1][26:].lower()
        if not tx or (tx.get("to") or "").lower() != DAI or tx["from"].lower() != frm:
            continue
        blk = int(l["blockNumber"], 16)
        for k, (d, n) in enumerate(decode_bet(memo_of(tx, PREFIX), blk)):
            out.append((blk, int(l["logIndex"], 16), frm, d, n, l["transactionHash"], k))  # k = 这笔里的第几个数字（排序用）
    return out


def read_ops(a, b):
    """极速池已经转回收款钱包的运营收入（没猜中的新票钱）：只认极速池钱包自己直接发、并且带「ailgns-fast-ops」的 DAI 转账。返回合计（wei）"""
    tot = 0
    for l in logs(DAI, a, b, [TRANSFER, pad(FAST_POOL), pad(SOURCE)]):
        tx = rpc("anubis", "eth_getTransactionByHash", [l["transactionHash"]])
        if not tx or (tx.get("to") or "").lower() != DAI or tx["from"].lower() != FAST_POOL:
            continue
        if memo_of(tx, OPS_PREFIX) is not None:
            tot += int(l["data"], 16)
    return tot


def read_paid(a, b):
    """已经发出去的奖：{"期号:钱包": [AiLGNS 数量, 交易]}。只认极速池钱包自己直接发、并且带「ailgns-fast-pay:期号」的 AiLGNS 转账"""
    out = {}
    for l in logs(AIL, a, b, [TRANSFER, pad(FAST_POOL)]):
        tx = rpc("anubis", "eth_getTransactionByHash", [l["transactionHash"]])
        if not tx or (tx.get("to") or "").lower() != AIL or tx["from"].lower() != FAST_POOL:
            continue
        rid = memo_of(tx, PAY_PREFIX)
        if rid and rid.isdigit():
            out[f"{rid}:0x{l['topics'][2][26:].lower()}"] = [str(int(l["data"], 16)), l["transactionHash"]]
    return out


# ───────── 记账本（状态）：同一套函数既给一次算完的试算用，也给每 5 分钟接着算的正式运行用 ─────────
def fresh(start):
    return {"v": STATE_V, "from": start, "to": start - 1, "bal": {}, "sold": 0, "names": 0, "carry": "0", "rounds": {}, "due": [], "paid": {}, "sent": [], "void": 0, "last": []}


def apply_events(st, tickets, bets, ts_of):
    """按（区块, 序号）先后把新票、新下注记进账本。返回作废的下注。
    票分两种：bal = 老票（旧规则），bal2 = 新票（新规则）。下注先用老票，不够再用新票；一期里的下注记成 [钱包, 数字, 老票数, 交易, 新票数]。"""
    ev = sorted([((t[0], t[1], t[6] if len(t) > 6 else 1), "T", t) for t in tickets] + [((x[0], x[1], 2, x[6] if len(x) > 6 else 0), "B", x) for x in bets], key=lambda z: z[0])
    void = []
    for key, kind, e in ev:
        blk = key[0]
        if kind == "T":
            key = "bal2" if (e[5] if len(e) > 5 else 1) == 2 else "bal"
            st.setdefault(key, {})
            st[key][e[2]] = st[key].get(e[2], 0) + e[4]; st["sold"] += e[4]; st["names"] += 1
            if key == "bal2":
                st["sold2"] = st.get("sold2", 0) + e[4]
            continue
        b2 = st.setdefault("bal2", {})
        rid = str(ts_of(blk) // ROUND)
        n1 = min(e[4], st["bal"].get(e[2], 0))
        used2 = sum((x[4] if len(x) > 4 else 0) for x in st["rounds"].get(rid, []) if x[0] == e[2])  # 这个钱包这一期已经押了多少新票
        n2 = max(0, min(e[4] - n1, b2.get(e[2], 0), CAP2 - used2))
        if n1 + n2 <= 0:
            void.append(e); st["void"] += 1; continue
        if n1:
            st["bal"][e[2]] -= n1
            if not st["bal"][e[2]]:
                del st["bal"][e[2]]
        if n2:
            b2[e[2]] -= n2
            if not b2[e[2]]:
                del b2[e[2]]
        st["rounds"].setdefault(rid, []).append([e[2], e[3], n1, e[5]] + ([n2] if n2 else []))
    return void


def settle_rounds(st, result_of):
    """按期号从小到大结算能结的期。result_of(期号) → (开奖数字, 开奖区块) 或 None（还没开）。返回这次结掉的各期明细。
    老票：这一期所有老票 × 0.9 + 滚存 = 老奖池，猜中的按票数分，没人中滚存。
    新票：猜中的每张返 0.9 DAI（只返自己的），没猜中的每张 0.9 DAI 记进 ops（运营收入，之后转回收款钱包）。"""
    detail = []
    for rid in sorted(int(k) for k in st["rounds"]):
        res = result_of(rid)
        if res is None:
            break
        bets = st["rounds"].pop(str(rid))
        stake = sum(x[2] for x in bets) * POT_PER_TICKET
        pot = int(st["carry"]) + stake
        win, win2, lose2, tk2 = {}, {}, 0, 0
        for x in bets:
            w, d, n, n2 = x[0], x[1], x[2], (x[4] if len(x) > 4 else 0)
            tk2 += n2
            if d == res[0]:
                if n:
                    win[w] = win.get(w, 0) + n
                if n2:
                    win2[w] = win2.get(w, 0) + n2
            else:
                lose2 += n2
        tot = sum(win.values())
        pay = {w: pot * n // tot for w, n in win.items()} if tot else {}
        st["carry"] = str(pot - sum(pay.values()))  # 老奖池：没人中整池滚存；有人中只剩分不尽的零头
        for w, n2 in win2.items():
            pay[w] = pay.get(w, 0) + n2 * WIN2_PER_TICKET
        ops = tk2 * POT_PER_TICKET - sum(win2.values()) * WIN2_PER_TICKET  # 这一期新票的运营收入：收进来的 − 赔出去的（可能是负数）
        if ops:
            st["ops"] = str(int(st.get("ops", "0")) + ops)
        for w, v in pay.items():
            if v > 0:
                st["due"].append({"r": rid, "w": w, "dai": str(v)})
        detail.append({"round": rid, "digit": res[0], "block": res[1], "tickets": stake // POT_PER_TICKET, "pot": pot, "win_tickets": tot, "pay": pay, "carry": int(st["carry"]), "bets": bets,
                       "tickets2": tk2, "win2": sum(win2.values()), "ret2": sum(win2.values()) * WIN2_PER_TICKET, "ops": ops})
        # 老票全用完了、老奖池里还剩滚存：已经没有人能再赢它，归运营收入（只在新规则生效后）
        if V2_START and int(st["carry"]) > 0 and not st["bal"] and not any(x[2] for b in st["rounds"].values() for x in b):
            st["ops"] = str(int(st.get("ops", "0")) + int(st["carry"])); detail[-1]["carry_to_ops"] = int(st["carry"]); st["carry"] = "0"
    return detail


def settle(tickets, bets, ts_of, result_of):
    """一次算完。返回 (剩余票, 各期明细, 滚存, 作废的下注)"""
    st = fresh(0)
    void = apply_events(st, tickets, bets, ts_of)
    detail = settle_rounds(st, result_of)
    for rid in sorted(int(k) for k in st["rounds"]):
        b = st["rounds"][str(rid)]
        stake = sum(x[2] for x in b) * POT_PER_TICKET
        detail.append({"round": rid, "open": True, "tickets": stake // POT_PER_TICKET, "pot": int(st["carry"]) + stake, "bets": b})
    return st["bal"], detail, int(st["carry"]), void


def liabilities(st):
    """按这份账，极速池现在应该备着多少 DAI（= 收进来还没花掉的钱）：没用的票（老票 + 新票）× 0.9 + 老奖池滚存 + 还没开奖的下注 × 0.9 + 中了还没发的 + 还没转回去的运营收入"""
    open_tk = sum(x[2] + (x[4] if len(x) > 4 else 0) for b in st["rounds"].values() for x in b)
    return (sum(st["bal"].values()) + sum(st.get("bal2", {}).values()) + open_tk) * POT_PER_TICKET + int(st["carry"]) + sum(int(d["dai"]) for d in st["due"]) + ops_due(st)


def worst_case(st):
    """最坏情况要备多少：所有没用的新票、没开奖的新票下注全都猜中（每张 1.9）。运营收入只在留够这个数以后才往外转，免得有人重仓猜中时池里没钱发"""
    open1 = sum(x[2] for b in st["rounds"].values() for x in b)
    open2 = sum((x[4] if len(x) > 4 else 0) for b in st["rounds"].values() for x in b)
    return (sum(st["bal"].values()) + open1) * POT_PER_TICKET + (sum(st.get("bal2", {}).values()) + open2) * WIN2_PER_TICKET + int(st["carry"]) + sum(int(d["dai"]) for d in st["due"])


def ops_due(st):
    """没猜中的新票钱，还有多少没转回收款钱包"""
    return max(0, int(st.get("ops", "0")) - int(st.get("ops_paid", "0")))


def reconcile(st):
    """链上已经发过的奖，从「待发」里划掉"""
    sent_keys = {f"{s['r']}:{s['w']}" for s in st["sent"]}
    keep = []
    for d in st["due"]:
        k = f"{d['r']}:{d['w']}"
        if k in st["paid"]:
            ail, tx = st["paid"].pop(k)
            st["sent"].append({"r": d["r"], "w": d["w"], "dai": d["dai"], "ail": ail, "tx": tx})
            sent_keys.add(k)
        else:
            keep.append(d)
    st["due"] = keep
    for k in [k for k in st["paid"] if k in sent_keys]:
        del st["paid"][k]
    st["sent"] = st["sent"][-500:]


# ───────── 发奖：买币 + 转账（只有 --run 而且不是 --dry-run 才会真的发交易） ─────────
def open_sells(cap):
    from otc_buyback import orders as read_orders
    now = time.time()
    out = [(o[6] / o[5], o) for o in read_orders(OTC) if o[2] and o[3] and o[7] > 0 and o[5] > 0 and o[4] > now + 60 and o[1].lower() != FAST_POOL and o[6] / o[5] <= cap]
    out.sort(key=lambda x: (x[0], x[1][0]))
    return out


def plan_buy(sells, need, budget):
    """走一遍卖单（最便宜的先吃）：要到手 need 个 AiLGNS（到手 = 成交 × 99%），最多花 budget 个 DAI。
    返回 [(单号, 成交数量, 付多少 DAI)]。一笔最少付 1 DAI，不够 1 DAI 的补到 1 DAI。"""
    out, spent, got = [], 0, 0
    for _, o in sells:
        if got >= need or budget - spent < 10**18:
            break
        oid, give_t, want_t, give_l = o[0], o[5], o[6], o[7]
        amount = min(give_l, -(-(need - got) * 100 // 99), (budget - spent) * give_t // want_t)
        pay = -(-amount * want_t // give_t)
        if pay < 10**18:
            amount = min(give_l, -(-10**18 * give_t // want_t))
            pay = -(-amount * want_t // give_t)
        while pay > budget - spent and amount > 0:
            amount -= 10**12
            pay = -(-amount * want_t // give_t)
        if pay < 10**18 or amount <= 0:
            continue
        out.append((oid, amount, pay))
        spent += pay
        got += amount * 99 // 100
    return out


def quote(sells, dai):
    """现在拿 dai 个 DAI 去 OTC 吃卖单，实际每枚 AiLGNS 多少 DAI（含 1% 手续费）；卖单不够返回 None"""
    plan = plan_buy(sells, MAX_U, max(dai, 10**18))
    spent, got = sum(p[2] for p in plan), sum(p[1] * 99 // 100 for p in plan)
    if got <= 0 or spent < min(dai, 10**18) * 99 // 100:
        return None
    return spent / got


def pay_out(st, pool, dry, fresh_paid, note):
    """把「待发」的奖发出去。返回本轮发了几笔"""
    todo = [d for d in st["due"] if f"{d['r']}:{d['w']}" not in fresh_paid]
    if not todo:
        return 0
    w = rpc("anubis", "eth_call", [{"to": AIL_PAIR, "data": "0x0902f1ac"}, "latest"])[2:]
    dex = int(w[64:128], 16) / int(w[:64], 16)
    px = 0.0
    try:
        import urllib.request
        r = json.load(urllib.request.urlopen(urllib.request.Request(SITE, headers={"user-agent": "ailgns-keeper"}), timeout=30))
        px = float(((r.get("funds") or {}).get("otc") or {}).get("px") or 0)
    except Exception:
        pass
    px = min(px, dex * BUY_CAP) if px else dex  # 本站 OTC 预言机价（读不到就用 DEX 现价）
    dai_bal, ail_bal = bal_of(DAI, FAST_POOL), bal_of(AIL, FAST_POOL)
    liab = liabilities(st)
    total = sum(int(d["dai"]) for d in todo)
    have = dai_bal + int(ail_bal * dex * 0.9) + 10**16
    if not V2_START and have < liab:  # 老规则：账上该备的钱比钱包里的多，说明账有问题，先停发查清楚
        note(f"⚠️ 极速池钱不够：按账应备 {liab / 1e18:.2f} DAI，钱包只有 {dai_bal / 1e18:.2f} DAI + {ail_bal / 1e18:.4f} AiLGNS。本轮不发奖。", True)
        return 0
    if V2_START and have < total + GAS_KEEP:  # 新规则：猜中的人拿的比他自己交的多，池里可能暂时垫不出。不打折，等钱够了再发
        note(f"⚠️ 极速池的钱不够发这一轮的奖：要发 {total / 1e18:.2f} DAI，钱包只有 {dai_bal / 1e18:.2f} DAI + {ail_bal / 1e18:.4f} AiLGNS。本轮不发，钱够了自动补发；急的话往极速池钱包转 DAI。", True)
        return 0
    if V2_START and have < liab:
        note(f"⚠️ 极速池的钱比账上该备的少：应备 {liab / 1e18:.2f} DAI，钱包 {dai_bal / 1e18:.2f} DAI + {ail_bal / 1e18:.4f} AiLGNS（有人猜中拿走的比他交的多）。奖照发，留意余额。", True)
    sells = open_sells(dex * BUY_CAP)
    q = quote(sells, total)
    use = max(px, q or 0)  # 卖单比预言机价贵 → 按实际买入价折，保证发出去的币都是奖池的钱买得起的
    px6 = int(round(use * 10**6))
    for d in todo:
        d["n"] = int(d["dai"]) * 10**6 // px6 // 10**12 * 10**12  # 向下取到 6 位小数
    need = sum(d["n"] for d in todo) - ail_bal
    print(f"待发 {len(todo)} 笔共 {total / 1e18:.4f} DAI；预言机价 {px:.4f}，OTC 实际买入价 {q and round(q, 4)}，按 {use:.4f} DAI/枚折 → {sum(d['n'] for d in todo) / 1e18:.6f} AiLGNS（钱包已有 {ail_bal / 1e18:.6f}）")
    if need > 0:
        budget = min(dai_bal - GAS_KEEP, int(total * BUY_SLACK) + 10**18)
        plan = plan_buy(sells, need, budget)
        if not plan:
            note(f"⚠️ 极速奖金 {total / 1e18:.2f} DAI 这轮没发：OTC 没有 ≤ {dex * BUY_CAP:.3f} DAI/枚 的卖单可买，下轮再试。", True)
        for oid, amount, pay in plan:
            if dry or not pool:
                print(f"（dry-run）会吃卖单 #{oid}：花 {pay / 1e18:.4f} DAI 买 {amount * 99 / 100 / 1e18:.6f} AiLGNS")
                continue
            from chains import send_tx
            if num(DAI, "0xdd62ed3e" + pad(FAST_POOL)[2:] + pad(OTC)[2:]) < pay:
                send_tx("anubis", pool, DAI, "0x095ea7b3" + pad(OTC)[2:] + f"{MAX_U:064x}", "极速池授权 OTC 用 DAI")
            try:
                h = send_tx("anubis", pool, OTC, "0x6fe4a448" + f"{oid:064x}" + f"{amount:064x}" + f"{pay:064x}", f"极速池买入 AiLGNS #{oid}")
                print(f"吃卖单 #{oid}：花 {pay / 1e18:.4f} DAI 买约 {amount * 99 / 100 / 1e18:.6f} AiLGNS {TX}{h}")
            except Exception as e:  # 被人抢先吃了等情况：跳过这张，下轮再补
                print(f"吃单 #{oid} 失败，跳过：{str(e)[:120]}")
    done = 0
    for d in sorted(todo, key=lambda d: (d["r"], d["w"])):
        n = d.pop("n")
        if n <= 0:
            continue
        if dry or not pool:
            print(f"（dry-run）会发：第 {d['r']} 期 {d['w']} {n / 1e18:.6f} AiLGNS（≈ {int(d['dai']) / 1e18:.4f} DAI）")
            continue
        if bal_of(AIL, FAST_POOL) < n:
            print(f"第 {d['r']} 期 {d['w'][:10]}… 的 {n / 1e18:.6f} AiLGNS 这轮币不够，下轮再发")
            continue
        from chains import send_tx
        h = send_tx("anubis", pool, AIL, "0xa9059cbb" + pad(d["w"])[2:] + f"{n:064x}" + (PAY_PREFIX + str(d["r"])).encode().hex(), "发极速预言家奖金")
        st["due"].remove(d)
        st["sent"].append({"r": d["r"], "w": d["w"], "dai": d["dai"], "ail": str(n), "tx": h})
        save(st)  # 发一笔存一笔
        done += 1
        print(f"✅ 第 {d['r']} 期 {hhmm(d['r'])} {d['w']}：{n / 1e18:.6f} AiLGNS（≈ {int(d['dai']) / 1e18:.4f} DAI） {TX}{h}")
    for d in todo:
        d.pop("n", None)
    return done


def save(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE + ".tmp", "w") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(STATE + ".tmp", STATE)


def run(dry, days):
    start = FAST_START
    if days:  # 试算：假设 N 天前上线，强制只读
        dry = True
        start = int(rpc("anubis", "eth_blockNumber", []), 16) - int(days * 86400)
    if not start:
        print("极速预言家还没上线（FAST_START 没填），什么都不做。")
        return
    latest = int(rpc("anubis", "eth_blockNumber", []), 16)
    safe = latest - CONFIRM
    if safe < start:
        print(f"还没到上线区块 {start}（现在 {latest}）。")
        return
    st = json.load(open(STATE)) if os.path.exists(STATE) and not days else fresh(start)
    if st.get("v") != STATE_V:  # 记票口径变过：从上线区块重新数一遍（发过的奖从链上记录认，不会重发）
        print(f"账本版本 {st.get('v')} → {STATE_V}：从区块 {start} 重新数")
        st = fresh(start)
    if st.get("from") != start:
        raise SystemExit(f"状态文件的起点 {st.get('from')} 和 FAST_START {start} 对不上，先查清楚再跑")
    pool = None
    if os.environ.get("FAST_POOL_KEY") and not dry:
        from eth_account import Account
        pool = Account.from_key(os.environ["FAST_POOL_KEY"])
        if pool.address.lower() != FAST_POOL:
            raise SystemExit("FAST_POOL_KEY 不是极速资金池钱包的私钥，停")
    alerts = []

    def note(msg, alert=False):
        print(msg)
        if alert:
            alerts.append(msg)

    # ① 接着上次读到的地方往后读：新票、新下注、已经发出去的奖
    a = st["to"] + 1
    if safe >= a:
        void = apply_events(st, read_tickets(a, safe), read_bets(a, safe), lambda b: block(b)[0])
        st["paid"].update(read_paid(a, safe))
        if V2_START:
            st["ops_paid"] = str(int(st.get("ops_paid", "0")) + read_ops(a, safe))  # 以链上为准：极速池已经转回收款钱包多少
        st["to"] = safe
        for v in void:
            print(f"作废下注：{v[2]} 猜 {v[3]} 压 {v[4]} 票（没票了） {v[5]}")

    # ② 结算：开奖区块已经过了 20 个确认的期
    def result_of(rid):
        n = first_block_at((rid + 1) * ROUND, safe)
        return (int(block(n)[1], 16) % 10, n) if n else None
    for x in settle_rounds(st, result_of):
        if x["pay"]:
            print(f"{hhmm(x['round'])} 期：开 {x['digit']}，{x['tickets']} 票，奖池 {x['pot'] / 1e18:.2f} DAI，中 {x['win_tickets']} 票 → " + "，".join(f"{w[:8]}… {v / 1e18:.4f}" for w, v in x["pay"].items()))
        else:
            print(f"{hhmm(x['round'])} 期：开 {x['digit']}，{x['tickets']} 票，没人中，{x['carry'] / 1e18:.2f} DAI 滚到下一期")
        st["last"] = (st.get("last") or [])[-49:] + [{"r": x["round"], "d": x["digit"], "b": x["block"], "tk": x["tickets"], "pot": str(x["pot"]), "win": x["win_tickets"]}]
    reconcile(st)

    # ③ 发奖。上一笔交易还卡在路上就先不发，免得重发
    fresh_paid = read_paid(safe + 1, latest) if latest > safe else {}
    cnt = lambda tag: int(rpc("anubis", "eth_getTransactionCount", [FAST_POOL, tag]), 16)
    if st["due"] and cnt("pending") != cnt("latest"):
        note("极速池钱包有交易还没确认，这轮先不发奖。")
    elif st["due"]:
        try:
            pay_out(st, pool, dry, fresh_paid, note)
        except Exception as e:
            note(f"⚠️ 极速发奖出错（下轮再试）：{str(e)[:200]}", True)

    # ④ 运营收入转回收款钱包：没猜中的新票钱攒够 OPS_MIN 才转；奖没发完、有交易在路上、池里的钱不够兜底都不转
    od = ops_due(st)
    if od >= OPS_MIN and not st["due"]:
        dai_bal, liab = bal_of(DAI, FAST_POOL), liabilities(st)
        if cnt("pending") != cnt("latest") or (latest > safe and read_ops(safe + 1, latest)):
            note("极速池钱包有交易还没确认，运营收入这轮先不转。")
        elif dai_bal < worst_case(st) + od + GAS_KEEP:  # 转完以后，池里还要够付「没用的新票全猜中」的最坏情况
            print(f"运营收入 {od / 1e18:.2f} DAI 这轮先不转：极速池钱包 {dai_bal / 1e18:.2f} DAI，要先留够最坏情况 {worst_case(st) / 1e18:.2f} + 2 DAI Gas。")
        elif dry or not pool:
            print(f"（dry-run）会把运营收入 {od / 1e18:.2f} DAI 从极速池转回收款钱包")
        else:
            try:
                from chains import send_tx
                h = send_tx("anubis", pool, DAI, "0xa9059cbb" + pad(SOURCE)[2:] + f"{od:064x}" + OPS_PREFIX.encode().hex(), "极速池运营收入转回收款钱包")
                # 不在这里记「已转」：已转多少只认链上日志（下一轮第 ① 步读到这笔才算），账本没存上也不会转两遍
                print(f"✅ 运营收入 {od / 1e18:.2f} DAI 已从极速池转回收款钱包 {TX}{h}")
            except Exception as e:
                note(f"⚠️ 运营收入没转成（下轮再试）：{str(e)[:160]}", True)

    dai_bal, liab = bal_of(DAI, FAST_POOL), liabilities(st)
    print(f"区块 {st['from']} → {st['to']}：卖出 {st['names']} 个域名 = {st['sold']} 票（其中新票 {st.get('sold2', 0)}），没用的老票 {sum(st['bal'].values())} 张、新票 {sum(st.get('bal2', {}).values())} 张；运营收入累计 {int(st.get('ops', '0')) / 1e18:.2f}、还没转回 {ops_due(st) / 1e18:.2f}；滚存 {int(st['carry']) / 1e18:.4f} DAI；待发 {len(st['due'])} 笔")
    print(f"极速池钱包 {dai_bal / 1e18:.2f} DAI + {bal_of(AIL, FAST_POOL) / 1e18:.6f} AiLGNS；按账应备 {liab / 1e18:.2f} DAI")
    if dry:
        return
    if alerts and time.time() - st.get("alert_t", 0) > 6 * 3600:  # 报警邮件最多 6 小时一封
        try:
            from notify_mail import send as send_mail
            ops = [e.strip() for e in (os.environ.get("OPS_EMAILS") or "").split(",") if e.strip()]
            send_mail("⚠️ 极速预言家要看一眼", "\n".join(alerts) + "\n\n—— AiLGNS 服务站", ops or None)
            st["alert_t"] = int(time.time())
        except Exception as e:
            print(f"报警邮件没发出去：{e}")
    save(st)


def hhmm(rid):
    return time.strftime("%m-%d %H:%M", time.gmtime(rid * ROUND + 8 * 3600))  # 北京时间


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="正式运行：记账、结算、发奖")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=float, default=0, help="假设多少天前上线（只用于试算，只读）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.run:
        return run(a.dry_run, a.days)
    latest = int(rpc("anubis", "eth_blockNumber", []), 16)
    now = block(latest)[0]

    def result_of(rid):
        n = first_block_at((rid + 1) * ROUND, latest)
        if n is None or latest - n < CONFIRM:
            return None
        return int(block(n)[1], 16) % 10, n

    if a.selftest:  # 假下注：最近 4 期，三个假钱包；开奖用真区块哈希
        r0 = now // ROUND - 5
        fake_blk = {1000 + i: (r0 + i) * ROUND + 30 for i in range(4)}
        A, B, C = "0xaaaa", "0xbbbb", "0xcccc"
        tickets = [(1, 0, A, "88888", 5), (1, 1, B, "12345", 5), (1, 2, C, "666", 640)]
        res = [result_of(r0 + i)[0] for i in range(4)]
        bets = [(1000, 0, A, (res[0] + 1) % 10, 2, "t1"), (1000, 1, B, (res[0] + 2) % 10, 1, "t2"),  # 第 1 期没人中 → 滚存 2.7
                (1001, 0, A, res[1], 1, "t3"), (1001, 1, B, res[1], 3, "t4"), (1001, 2, C, (res[1] + 1) % 10, 10, "t5"),  # 第 2 期 A、B 按 1:3 分
                (1002, 0, A, res[2], 9, "t6"),  # A 只剩 2 张，按 2 张算，独中
                (1003, 0, A, res[3], 1, "t7")]  # A 没票了，作废
        bal, detail, carry, void = settle(tickets, bets, lambda b: fake_blk[b], result_of)
        show(bal, detail, carry, void, tickets)
        d = {x["round"] - r0: x for x in detail}
        assert d[0]["pay"] == {} and d[0]["carry"] == 27 * 10**17
        assert d[1]["pot"] == 27 * 10**17 + 14 * POT_PER_TICKET and d[1]["pay"] == {A: d[1]["pot"] // 4, B: d[1]["pot"] * 3 // 4}
        assert d[2]["pay"] == {A: d[2]["pot"]} and d[2]["tickets"] == 2 and len(void) == 1 and bal == {B: 1, C: 630}
        # 接着算（一期一期喂）和一次算完，结果必须一模一样；发过的奖要能从链上记录划掉
        st = fresh(0)
        for i in range(4):
            apply_events(st, tickets if i == 0 else [], [x for x in bets if x[0] == 1000 + i], lambda b: fake_blk[b])
            settle_rounds(st, lambda rid: result_of(rid) if rid <= r0 + i - 1 else None)
        settle_rounds(st, result_of)
        assert st["bal"] == bal and int(st["carry"]) == carry and not st["rounds"]
        want = sorted((x["round"], w, v) for x in detail for w, v in x.get("pay", {}).items())
        assert sorted((x["r"], x["w"], int(x["dai"])) for x in st["due"]) == want and len(want) == 3
        tot_in = sum(min(x[4], 99) for x in bets[:5]) * POT_PER_TICKET + 2 * POT_PER_TICKET
        assert sum(v for _, _, v in want) + carry == tot_in, "进奖池的钱 = 发出去的 + 滚存"
        assert liabilities(st) == (1 + 630) * POT_PER_TICKET + carry + sum(v for _, _, v in want)
        st["paid"][f"{r0 + 1}:{A}"] = ["123", "0xpaid"]
        reconcile(st)
        assert len(st["due"]) == 2 and st["sent"][-1]["tx"] == "0xpaid" and not st["paid"]
        # 买币计划：两张假卖单（1.00 和 1.10 DAI/枚），要到手 5 枚
        E = 10**18
        sells = [(1.0, (7, "0xm", True, True, 0, 3 * E, 3 * E, 3 * E)), (1.1, (8, "0xm", True, True, 0, 10 * E, 11 * E, 10 * E))]
        plan = plan_buy(sells, 5 * E, 100 * E)
        assert plan[0] == (7, 3 * E, 3 * E) and plan[1][0] == 8 and sum(p[1] * 99 // 100 for p in plan) >= 5 * E > sum(p[1] * 99 // 100 for p in plan) - 10**13
        assert plan_buy(sells, 5 * E, 3 * E + E // 2) == [(7, 3 * E, 3 * E)], "预算不够 1 DAI 就不再吃下一张"
        small = plan_buy(sells, E // 10, 100 * E)
        assert small == [(7, E, E)], "不到 1 DAI 的补到 1 DAI"
        q = quote(sells, 5 * E)
        assert q and 1.01 < q < 1.12
        # —— 新规则（2026-11-14）——
        # 哪些算票：同一个钱包 600 个区块内注册 2 个以上
        g = pair_grants([(100, 0, A), (400, 3, A), (2000, 0, A), (100, 1, B), (701, 0, B), (100, 2, C), (300, 0, C), (900, 5, C)])
        assert g == {(100, 0): (400, 3, 0), (400, 3): (400, 3, 1), (100, 2): (300, 0, 0), (300, 0): (300, 0, 1), (900, 5): (900, 5, 1)}, g
        # 返奖：A 老票 2 张 + 新票 5 张，B 新票 5 张，C 新票 10 张
        st = fresh(0)
        tk2 = [(1, 0, A, "88888", 2), (2, 5, A, "apple", 5, 2, 0), (2, 5, B, "pear", 5, 2, 1), (3, 0, C, "一二三", 10, 2, 1)]
        b2 = [(1000, 0, A, res[0], 4, "u1"),            # A 压 4 张：先用 2 张老票，再用 2 张新票，猜中
              (1000, 1, B, (res[0] + 1) % 10, 5, "u2"),  # B 5 张新票，没中 → 4.5 DAI 归运营
              (1000, 2, C, res[0], 3, "u3"),             # C 3 张新票，猜中 → 只返 2.7，不分 B 的钱
              (1001, 0, C, (res[1] + 1) % 10, 7, "u4")]  # C 剩下 7 张，没中 → 6.3 归运营
        void = apply_events(st, tk2, b2, lambda b: fake_blk[b])
        assert not void and st["bal"] == {} and st["bal2"] == {A: 3} and st["sold"] == 22 and st["sold2"] == 20
        assert st["rounds"][str(r0)][0] == [A, res[0], 2, "u1", 2] and st["rounds"][str(r0)][1] == [B, (res[0] + 1) % 10, 0, "u2", 5]
        assert liabilities(st) == (3 + 4 + 5 + 3 + 7) * POT_PER_TICKET
        dd = {x["round"] - r0: x for x in settle_rounds(st, result_of)}
        W2 = WIN2_PER_TICKET
        assert dd[0]["pay"] == {A: 2 * POT_PER_TICKET + 2 * W2, C: 3 * W2}, dd[0]["pay"]  # A：老奖池 1.8（只有他的老票）+ 新票 2 张 × 1.9；C：3 张 × 1.9 = 5.7，不分 B 的钱
        assert dd[0]["ops"] == 10 * POT_PER_TICKET - 5 * W2 and dd[0]["carry"] == 0 and dd[0]["tickets"] == 2 and dd[0]["tickets2"] == 10 and dd[0]["win2"] == 5  # 这一期运营收入 9 − 9.5 = −0.5（赔的比收的多）
        assert dd[1]["pay"] == {} and dd[1]["ops"] == 7 * POT_PER_TICKET and dd[1]["carry"] == 0
        assert int(st["ops"]) == 17 * POT_PER_TICKET - 5 * W2 and ops_due(st) == 17 * POT_PER_TICKET - 5 * W2  # 累计 15.3 − 9.5 = 5.8
        paid_out = sum(int(d["dai"]) for d in st["due"])
        assert paid_out + int(st["ops"]) + 3 * POT_PER_TICKET == 22 * POT_PER_TICKET, "进极速池的钱（票钱的 90%）= 发给猜中的 + 运营收入 + 还没用的票"
        assert liabilities(st) == 3 * POT_PER_TICKET + paid_out + ops_due(st)
        assert worst_case(st) == 3 * W2 + paid_out
        st["ops_paid"] = str(4 * POT_PER_TICKET)
        assert ops_due(st) == 13 * POT_PER_TICKET - 5 * W2 and liabilities(st) == 3 * POT_PER_TICKET + paid_out + 13 * POT_PER_TICKET - 5 * W2
        st2 = fresh(0)  # 一个人重仓猜中：运营收入是负的，不往外转
        apply_events(st2, [(2, 0, C, "a", 10, 2, 1)], [(1000, 0, C, res[0], 10, "v1")], lambda b: fake_blk[b])
        settle_rounds(st2, result_of)
        assert st2["due"][0]["dai"] == str(10 * W2) and int(st2["ops"]) == 10 * POT_PER_TICKET - 10 * W2 and ops_due(st2) == 0
        print("\n✅ 验算通过（一次算完 = 接着算；钱进出对得上；买币计划对；新规则：批量才算票、猜中每张发 1.9、没中的归运营）")
        return

    start = FAST_START if FAST_START and not a.days else latest - int((a.days or 3) * 86400)
    tickets = read_tickets(start, latest)
    bets = read_bets(start, latest)
    bal, detail, carry, void = settle(tickets, bets, lambda b: block(b)[0], result_of)
    pool = bal_of(DAI, FAST_POOL)
    if a.json:
        print(json.dumps({"from": start, "to": latest, "tickets_left": bal, "rounds": detail, "carry": str(carry), "pool_dai": str(pool)}, default=str, ensure_ascii=False))
        return
    print(f"区块 {start} → {latest}" + ("" if FAST_START and not a.days else f"（试算：假设 {a.days or 3:g} 天前上线）"))
    show(bal, detail, carry, void, tickets)
    need = sum(bal.values()) * POT_PER_TICKET + carry + sum(x["pot"] - carry for x in detail if x.get("open"))
    print(f"\n极速池钱包现有 {pool / 1e18:.2f} DAI；按这份账应备 {need / 1e18:.2f} DAI（没用的票 × 0.9 + 滚存 + 未开奖的池）")
    print("\n最近 12 期开奖（真区块哈希 % 10）：")
    cur = now // ROUND
    for rid in range(cur - 12, cur):
        r = result_of(rid)
        print(f"  {hhmm(rid)} 期 → " + (f"{r[0]}（区块 {r[1]}）" if r else "等开奖"))


def show(bal, detail, carry, void, tickets):
    sold = sum(t[4] for t in tickets)
    print(f"卖出 {len(tickets)} 个纯数字域名 = {sold} 票；还没用的 {sum(bal.values())} 票，分在 {sum(1 for v in bal.values() if v)} 个钱包")
    for t in tickets[-8:]:
        print(f"  {t[3]}.ailgns  {t[2][:8]}…  +{t[4]} 票")
    for x in detail:
        if x.get("open"):
            print(f"{hhmm(x['round'])} 期：{x['tickets']} 票已下注，奖池 {x['pot'] / 1e18:.2f} DAI，等开奖")
        elif x["pay"]:
            print(f"{hhmm(x['round'])} 期：开 {x['digit']}，{x['tickets']} 票，奖池 {x['pot'] / 1e18:.2f} DAI，中 {x['win_tickets']} 票 → " + "，".join(f"{w[:8]}… {v / 1e18:.4f}" for w, v in x["pay"].items()))
        else:
            print(f"{hhmm(x['round'])} 期：开 {x['digit']}，{x['tickets']} 票，没人中，{x['carry'] / 1e18:.2f} DAI 滚到下一期")
    print(f"当前滚存 {carry / 1e18:.4f} DAI；作废下注 {len(void)} 笔")


if __name__ == "__main__":
    main()
