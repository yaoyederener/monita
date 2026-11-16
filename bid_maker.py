"""📝 AiLGNS 做市买单 · 空跑版（2026-11-16 站长定）：只读链、只记账，不下单、不动钱。

想法：在本站 OTC 常挂一张买单，出价 = 官方 DEX 卖出实际到手价 × (1 − 安全余量)；有人卖给这张单，
     机器人马上把收到的 AiLGNS 拿去 DEX 卖掉。机器人这头不亏（买价 < DEX 到手价），卖家付的 1% 手续费
     照常进收入钱包。效果是把 OTC 的买一价顶到贴近 DEX 到手价。

这个文件**没有私钥、没有任何发交易的代码**，每次运行只做三件事：
  1. 读 DEX 池子，算「卖 SIZE 枚实际到手多少」→ 这一轮该挂什么价
  2. 读 OTC 盘口，看这个价是不是买一；如果高过了卖一，就压到卖一下面一分钱
  3. 读上一轮到现在 OTC 上真实发生的「卖给买单」的成交：成交价低于我们上一轮挂价的，
     算作「如果我们的单挂着，这些人会卖给我们」→ 记一笔模拟账（每轮最多 SIZE 枚）
结果存 data/bid_maker.json。跑几天看：一天能接多少、机器人这头赚多少、手续费多少、有没有哪一轮会亏。

    python bid_maker.py          # 跑一轮
参数（环境变量）：BID_SIZE 每轮最多接多少枚（默认 5）；BID_MARGIN 安全余量（默认 0.002 = 0.2%）

DEX 到手率：2026-11-16 用链上真实卖出核过——卖出先扣 3% 税，剩下 97% 进池子按 0.3% 手续费换 DAI
（1 枚到手 ≈ 池子价 × 0.967）。data/arb.json 里 11-05 测的 0.8485 已过时。上真钱之前必须用小额再实测一次。
"""
import json
import os
import time

from ailgns_lp_monitor import rpc

OTC = "0x4e730be6d46be71223ded5a32dc45799f6d4bc15"   # AiLGNS OTC 托管合约
PAIR = "0x0a1cce65d995c6c5100e6f5ba3c388ef34cecaf0"  # 官方 DEX AiLGNS/DAI 池（token0 = AiLGNS）
COUNT, GET_ORDERS, RESERVES = "0x35daa731", "0x8f72fc77", "0x0902f1ac"
FILLED = "0x69c74c6fcd53500d036d8a37cc9514fb69b660b99d36728e8aba82e7a53b055d"  # Filled(id, taker, amount, paid, fee, giveLeft)
SELL_TAX = 0.03      # AiLGNS 卖进池子先扣 3%
POOL_FEE = 0.003     # 池子手续费
OTC_FEE = 0.01       # OTC 吃单手续费（合约常数，进收入钱包）
SIZE = float(os.environ.get("BID_SIZE", "5"))
MARGIN = float(os.environ.get("BID_MARGIN", "0.002"))
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bid_maker.json")
MAX_RANGE = 4999     # 节点一次最多查 5000 个块的日志
KEEP = 400           # 最多留多少条明细


def call(to, data):
    return rpc("eth_call", [{"to": to, "data": data}, "latest"])


def reserves():
    w = call(PAIR, RESERVES)[2:]
    return int(w[:64], 16) / 1e18, int(w[64:128], 16) / 1e18  # (AiLGNS, DAI)


def dex_net(x, r_tok, r_dai):
    """卖 x 枚 AiLGNS 实际到手多少 DAI：先扣税，再按 x*y=k 换"""
    a = x * (1 - SELL_TAX) * (1 - POOL_FEE)
    return a * r_dai / (r_tok + a)


def book():
    """最近 1000 张挂单 → {id: (卖币?, open, expiry, give, want, left)}"""
    n = int(call(OTC, COUNT), 16)
    frm = max(0, n - 1000)
    h = call(OTC, GET_ORDERS + f"{frm:064x}" + f"{n - frm:064x}")[2:]
    w = [h[i:i + 64] for i in range(0, len(h), 64)]
    out = {}
    for i in range(int(w[1], 16)):
        o = w[2 + i * 7:9 + i * 7]
        if len(o) < 7:
            break
        out[frm + i] = (int(o[1], 16) == 1, int(o[2], 16) == 1, int(o[3], 16), int(o[4], 16), int(o[5], 16), int(o[6], 16))
    return out


def price(o):
    sell, _, _, give, want, _ = o
    return (want / give) if sell else (give / want)  # 两边都是 18 位小数


def load():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"since": int(time.time()), "block": 0, "bid": 0, "runs": 0, "top_runs": 0,
                "coins": 0.0, "bot_pnl": 0.0, "fee": 0.0, "worst": None, "seen_sell_coins": 0.0, "log": []}


def main():
    st = load()
    now = int(time.time())
    head = int(rpc("eth_blockNumber", []), 16)
    r_tok, r_dai = reserves()
    net = dex_net(SIZE, r_tok, r_dai) / SIZE                # 卖 SIZE 枚，平均每枚到手
    bid = int(net * (1 - MARGIN) * 100) / 100               # 往下取到分
    bk = book()
    live = [o for o in bk.values() if o[1] and o[2] > now and o[5] > 0]
    bids = sorted((price(o) for o in live if not o[0] and o[3] * 1e-18 >= 1), reverse=True)
    asks = sorted(price(o) for o in live if o[0])
    best_bid, best_ask = (bids[0] if bids else 0), (asks[0] if asks else 0)
    # 不能挂到卖一之上（买单 ≥ 卖单会被撮合掉，等于按卖一价吃单）：这时压到卖一下面一分钱。压低了更不会亏
    crossed = bool(best_ask) and bid >= best_ask
    if crossed:
        bid = int(round((best_ask - 0.01) * 100)) / 100
    top = bid > best_bid

    # 上一轮到现在，真实发生的「卖给买单」
    sim_coins = sim_pnl = sim_fee = seen = 0.0
    prev_bid = st.get("bid") or 0
    frm = max(st.get("block", 0) + 1, head - MAX_RANGE)
    if st.get("block") and prev_bid:
        logs = rpc("eth_getLogs", [{"address": OTC, "topics": [FILLED], "fromBlock": hex(frm), "toBlock": hex(head)}])
        cap = SIZE
        for l in logs:
            o = bk.get(int(l["topics"][1], 16))
            if not o or o[0]:
                continue  # 只看买单被吃（= 有人卖币）
            d = l["data"][2:]
            got_dai, paid_tok = int(d[:64], 16) / 1e18, int(d[64:128], 16) / 1e18
            if paid_tok <= 0:
                continue
            seen += paid_tok
            px = got_dai / paid_tok
            if px >= prev_bid or cap <= 0:
                continue  # 人家卖到了更高的价，不会来找我们；或这一轮的量已经接满
            c = min(paid_tok, cap)
            cap -= c
            sim_coins += c
            sim_pnl += c * (dex_net(c, r_tok, r_dai) / c - prev_bid)   # 按现在的池子价卖掉
            sim_fee += c * prev_bid * OTC_FEE

    st["runs"] = st.get("runs", 0) + 1
    st["top_runs"] = st.get("top_runs", 0) + (1 if top else 0)
    st["coins"] = round(st.get("coins", 0) + sim_coins, 6)
    st["bot_pnl"] = round(st.get("bot_pnl", 0) + sim_pnl, 4)
    st["fee"] = round(st.get("fee", 0) + sim_fee, 4)
    st["seen_sell_coins"] = round(st.get("seen_sell_coins", 0) + seen, 6)
    if sim_coins and (st.get("worst") is None or sim_pnl / sim_coins < st["worst"]):
        st["worst"] = round(sim_pnl / sim_coins, 4)  # 最差的一轮：机器人这头每枚赚 / 亏多少
    row = {"t": now, "blk": head, "spot": round(r_dai / r_tok, 3), "net": round(net, 3), "bid": bid,
           "best_bid": round(best_bid, 3), "best_ask": round(best_ask, 3), "top": top, "crossed": crossed}
    if sim_coins:
        row.update({"coins": round(sim_coins, 6), "pnl": round(sim_pnl, 4), "fee": round(sim_fee, 4)})
    st["log"] = (st.get("log", []) + [row])[-KEEP:]
    st.update({"block": head, "bid": bid, "crossed": crossed, "size": SIZE, "margin": MARGIN, "at": now})
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)

    days = max((now - st["since"]) / 86400, 1e-9)
    print(f"[空跑] DEX 池子价 {r_dai / r_tok:.3f} · 卖 {SIZE:g} 枚每枚到手 {net:.3f} · 这一轮该挂 {bid:.2f}"
          f"（余量 {MARGIN * 100:.2f}%）· OTC 买一 {best_bid:.2f} / 卖一 {best_ask:.2f} · "
          + ("我们是买一 ✅" if top else "别人出价更高，排不到第一") + ("（原本该挂得更高，被卖一压住了）" if crossed else ""))
    if sim_coins:
        print(f"[空跑] 这一轮模拟接到 {sim_coins:.4f} 枚：机器人这头 {sim_pnl:+.3f} DAI，手续费 +{sim_fee:.3f} DAI")
    print(f"[空跑] 累计 {days:.2f} 天 / {st['runs']} 轮：模拟接 {st['coins']:.4f} 枚（期间 OTC 上共有 {st['seen_sell_coins']:.4f} 枚卖给买单）· "
          f"机器人这头 {st['bot_pnl']:+.2f} DAI · 手续费 +{st['fee']:.2f} DAI · 当买一的轮数 {st['top_runs']}/{st['runs']}"
          + (f" · 最差一轮每枚 {st['worst']:+.3f}" if st.get("worst") is not None else ""))


if __name__ == "__main__":
    main()
