"""⚡ 极速预言家：只读算账（第 1 步，站长 2026-11-12 定稿）。这个文件不发任何交易、不碰任何私钥。

规则：
  一期 10 分钟，整点对齐（:00、:10、:20…）。期号 = 区块时间戳 // 600。
  开奖 = 本期结束后第一个区块的哈希 % 10（0 到 9）。
  票   = 上线之后新注册的【纯数字 .ailgns 域名】，付了多少 DAI 就是多少票（1 DAI = 1 票，按付款钱包记）。续费不算票。
  下注 = 持票钱包给极速资金池转 0 个 DAI（只花 Gas），交易数据末尾带「ailgns-fast:数字:票数」。
         这样下注会留下一条 Transfer 日志，机器人按日志就能找全，不靠网站登记，谁也漏不掉、改不了。
         票数超过剩余的，按剩余算；一张不剩的作废。下注算进交易所在区块的那一期。
  分钱 = 每张下注的票带 0.9 DAI 进本期奖池（另 0.1 归平台），猜中的按票数平分；没人中，整池滚到下一期。
    python fast_oracle.py --days 3      # 假设 3 天前上线，用真实链上数据算一遍
    python fast_oracle.py --selftest    # 用假下注 + 真区块哈希验算分钱
"""
import argparse
import json
import re

from chains import rpc

FAST_POOL = "0x8610bf581b1c10cb7d372f93b8cdcde382a7c1d1"
DAI = "0x83fd06f0846d9d90b3016bf670efe2e0b11cde14"
NAMES_AIL = "0xbc408e9d7350967e7247c917a086cb4ece0ed988"  # 只认 .ailgns
PAID = "0x26c4308021cef5cdbc618fee8b61faf9de6b8f7e4e49fac43869dc1a26756408"  # Paid(payer, token, amount, costDai)
REG = "0xa15b66031269f1c32be8db1e4353108dc76c84574a7c6697527e431ad921462a"  # NameRegistered(id, label, owner, cost, expires)
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
OWN = {"0xedb9c6900a11d2dd37da6b59f3604c35a39db1e6", "0x89dd06754bb239b6c4c92bc47c8fa72889084e27"}  # 站长自己付的不算票
FAST_START = None  # 上线区块。上线那天填，之前注册的纯数字域名一律不算票
ROUND = 600
TICKET_WEI = 10**18
POT_PER_TICKET = 9 * 10**17  # 每张票 0.9 DAI 进奖池
PREFIX = "ailgns-fast:"
CONFIRM = 20  # 开奖区块要再等 20 个块才结算，防链重组
BET_RE = re.compile(r"^([0-9]):([1-9][0-9]{0,8})$")


def pad(a):
    return "0x" + a.lower().replace("0x", "").rjust(64, "0")


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


def read_tickets(a, b):
    """[(区块, 序号, 钱包, 域名, 票数)]：同一笔交易里 Paid 紧跟着 NameRegistered 才算（续费只有 Paid，不算）"""
    out, last_paid = [], {}
    ls = logs(NAMES_AIL, a, b, [[PAID, REG]])
    ls.sort(key=lambda l: (int(l["blockNumber"], 16), int(l["logIndex"], 16)))
    for l in ls:
        tx = l["transactionHash"]
        if l["topics"][0] == PAID:
            last_paid[tx] = ("0x" + l["topics"][1][26:].lower(), int(l["data"][2 + 64:2 + 128], 16))
            continue
        paid = last_paid.pop(tx, None)
        lab = label_of(l["data"])
        if not paid or paid[0] in OWN or not re.fullmatch(r"[0-9]+", lab):
            continue
        n = paid[1] // TICKET_WEI
        if n > 0:
            out.append((int(l["blockNumber"], 16), int(l["logIndex"], 16), paid[0], lab, n))
    return out


def read_bets(a, b):
    """[(区块, 序号, 钱包, 数字, 票数, 交易)]：给极速池转 0 DAI，且交易是钱包自己直接发给 DAI 合约的"""
    out = []
    for l in logs(DAI, a, b, [TRANSFER, None, pad(FAST_POOL)]):
        if int(l["data"], 16) != 0:
            continue
        tx = rpc("anubis", "eth_getTransactionByHash", [l["transactionHash"]])
        frm = "0x" + l["topics"][1][26:].lower()
        inp = (tx or {}).get("input", "")
        if not tx or (tx.get("to") or "").lower() != DAI or tx["from"].lower() != frm or not inp.startswith("0xa9059cbb") or len(inp) <= 138:
            continue
        try:
            txt = bytes.fromhex(inp[138:]).decode("utf-8")
        except Exception:
            continue
        m = BET_RE.match(txt[len(PREFIX):]) if txt.startswith(PREFIX) else None
        if m:
            out.append((int(l["blockNumber"], 16), int(l["logIndex"], 16), frm, int(m.group(1)), int(m.group(2)), l["transactionHash"]))
    return out


def settle(tickets, bets, ts_of, result_of):
    """纯算账。ts_of(区块) → 时间戳；result_of(期号) → (开奖数字, 开奖区块) 或 None（还没开）
    返回 (剩余票, 各期明细, 滚存, 作废的下注)"""
    ev = sorted([(t[0], t[1], "T", t) for t in tickets] + [(x[0], x[1], "B", x) for x in bets])
    bal, rounds, void = {}, {}, []
    for blk, _, kind, e in ev:
        if kind == "T":
            bal[e[2]] = bal.get(e[2], 0) + e[4]
            continue
        n = min(e[4], bal.get(e[2], 0))
        if n <= 0:
            void.append(e); continue
        bal[e[2]] -= n
        rounds.setdefault(ts_of(blk) // ROUND, []).append((e[2], e[3], n, e[5]))
    carry, detail = 0, []
    for rid in sorted(rounds):
        res = result_of(rid)
        stake = sum(x[2] for x in rounds[rid]) * POT_PER_TICKET
        if res is None:
            detail.append({"round": rid, "open": True, "tickets": stake // POT_PER_TICKET, "pot": carry + stake, "bets": rounds[rid]})
            continue
        pot = carry + stake
        win = {}
        for w, d, n, _ in rounds[rid]:
            if d == res[0]:
                win[w] = win.get(w, 0) + n
        tot = sum(win.values())
        pay = {w: pot * n // tot for w, n in win.items()} if tot else {}
        carry = pot - sum(pay.values())  # 没人中：整池滚存；有人中：只剩分不尽的零头
        detail.append({"round": rid, "digit": res[0], "block": res[1], "tickets": stake // POT_PER_TICKET, "pot": pot, "win_tickets": tot, "pay": pay, "carry": carry, "bets": rounds[rid]})
    return bal, detail, carry, void


def hhmm(rid):
    import time
    return time.strftime("%m-%d %H:%M", time.gmtime(rid * ROUND + 8 * 3600))  # 北京时间


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=0, help="假设多少天前上线（只用于试算）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
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
        assert d[2]["pay"] == {A: d[2]["pot"]} and d[2]["tickets"] == 2 and len(void) == 1 and bal == {A: 0, B: 1, C: 630}
        print("\n✅ 验算通过")
        return

    start = FAST_START if FAST_START and not a.days else latest - int((a.days or 3) * 86400)
    tickets = read_tickets(start, latest)
    bets = read_bets(start, latest)
    bal, detail, carry, void = settle(tickets, bets, lambda b: block(b)[0], result_of)
    pool = int(rpc("anubis", "eth_call", [{"to": DAI, "data": "0x70a08231" + pad(FAST_POOL)[2:]}, "latest"]), 16)
    if a.json:
        print(json.dumps({"from": start, "to": latest, "tickets_left": bal, "rounds": detail, "carry": str(carry), "pool_dai": str(pool)}, default=str, ensure_ascii=False))
        return
    print(f"区块 {start} → {latest}" + ("" if FAST_START and not a.days else f"（试算：假设 {a.days or 3:g} 天前上线）"))
    show(bal, detail, carry, void, tickets)
    need = sum(bal.values()) * POT_PER_TICKET + carry + sum(x["pot"] for x in detail if x.get("open"))
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
