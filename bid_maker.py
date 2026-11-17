"""📝 AiLGNS 做市买单 · 空跑版（2026-11-16 站长定）：只读链、只记账，不下单、不动钱。

想法：在本站 OTC 常挂一张买单，出价 = 官方 DEX 卖出实际到手价 × (1 − 安全余量)；有人卖给这张单，
     机器人马上把收到的 AiLGNS 拿去 DEX 卖掉。机器人这头不亏（买价 < DEX 到手价），卖家付的 1% 手续费
     照常进收入钱包。效果是把 OTC 的买一价顶到贴近 DEX 到手价。

**默认只空跑**（下面三件事，只读链）。仓库变量 BID_ON=1 时才另外跑文件最后的「实盘」部分。空跑每次运行做三件事：
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
    asks = sorted(price(o) for o in live if o[0] and o[5] * o[4] // o[3] >= 10**18)  # 不到 1 DAI 的渣单不算
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


# ───────────────────────── 实盘（BID_ON=1 才走；默认关） ─────────────────────────
# 钱包用仓库里已有的「做市 / 套利钱包」0x06cc…4437（Secret ARB_KEY）。只当普通用户：挂单 / 撤单 / 在官方路由卖币，不碰任何管理功能。
# 每一轮按顺序做：
#   ① 钱包里有 AiLGNS（= 买单被人吃了）→ 走官方路由一笔卖掉，最低到手 = 买进成本，低于成本这笔交易自己回滚（卖不亏）
#   ② 算总资产（DAI + 挂单里的 DAI + 手里的币按 DEX 到手价），比最高点回撤超过 BID_MAX_LOSS → 撤单停机
#   ③ 买单价不对 / 快过期 / 被吃得差不多了 → 撤掉重挂。挂单有效期只有 30 分钟：机器人万一停了，单子自己过期，不会留一张没人管的旧价单
# 任何一步出错：撤单 + 停机（state.halted），要人看过之后改仓库变量 BID_RESET 才会重新开始。
ME = "0x06cc2a46d7ad17496c2de9c61362808e06b44437"
DAI = "0x83fd06f0846d9d90b3016bf670efe2e0b11cde14"
AIL = "0x256edb652db43e721ec6a12b6b51b6967d3bb8cb"
ROUTER = "0x3e412e02b6157fbe80b6c6697f3ce1142e629019"  # 官方路由（UniswapV2 式，2026-11-16 看过普通钱包在这里卖币）
CREATE, CANCEL, SWAP_FOT = "0x9128a740", "0x40e58ee5", "0x5c11d795"
GAS_KEEP = 2 * 10**18          # 永远留 2 DAI 付 Gas（Anubis 的 Gas 就是 DAI）
MIN_ORDER = 20 * 10**18        # 本金不到 20 DAI 就不挂
MIN_SELL = 10**15              # 手里的币不到 0.001 枚不值得卖
DURATION = 1800                # 挂单有效期 30 分钟
MAX_LOSS = float(os.environ.get("BID_MAX_LOSS", "20"))
MAX_U = (1 << 256) - 1
TX = "https://anubisscan.io/tx/"


def pad(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def bal(tok, who):
    return int(call(tok, "0x70a08231" + pad(who)), 16)


def my_orders(now):
    """我自己还开着的买单：[(id, 单价, 剩下的 DAI wei, 过期时间)]"""
    n = int(call(OTC, COUNT), 16)
    frm = max(0, n - 1000)
    h = call(OTC, GET_ORDERS + f"{frm:064x}" + f"{n - frm:064x}")[2:]
    w = [h[i:i + 64] for i in range(0, len(h), 64)]
    mine, others_bid, others_ask = [], 0.0, 0.0
    for i in range(int(w[1], 16)):
        o = w[2 + i * 7:9 + i * 7]
        if len(o) < 7:
            break
        maker, sell, opn, exp = "0x" + o[0][24:], int(o[1], 16) == 1, int(o[2], 16) == 1, int(o[3], 16)
        give, want, left = int(o[4], 16), int(o[5], 16), int(o[6], 16)
        if not opn or left == 0 or not give or not want:
            continue
        if maker == ME:
            if not sell:
                mine.append((frm + i, give / want, left, exp))  # 过期没过期都算：过期的也要撤回来才拿得到钱
            continue
        if exp <= now:
            continue
        if sell:
            p = want / give
            if left * want // give < 10**18:
                continue  # 剩下不到 1 DAI 的卖单是成交剩的渣（页面上也不显示），不能让它把买单价压住（2026-11-17 实盘第一轮就被两张 820.05 的渣单压到 820.04）
            others_ask = p if not others_ask else min(others_ask, p)
        elif give >= 10**18:
            others_bid = max(others_bid, give / want)
    return mine, others_bid, others_ask


def live(st):
    from eth_account import Account
    from chains import send_tx
    key = os.environ.get("ARB_KEY", "").strip()
    if not key:
        raise SystemExit("BID_ON=1 但没有 ARB_KEY，实盘不跑")
    acct = Account.from_key(key)
    if acct.address.lower() != ME:
        raise SystemExit("ARB_KEY 对应的地址不是做市钱包 0x06cc…4437，不跑")
    now = int(time.time())
    reset = os.environ.get("BID_RESET", "")
    if st.get("halted") and reset and reset != st.get("reset"):
        print(f"[实盘] 收到 BID_RESET={reset}：解除停机，从现在的资产重新算起")
        st.pop("halted", None); st.pop("peak", None); st.pop("equity", None)
    st["reset"] = reset

    def tx(to, data, label):
        h = send_tx("anubis", acct, to, data, label)
        print(f"[实盘] {label} ✅ {TX}{h}")
        return h

    def cancel_all(mine):
        for oid, _, _, _ in mine:
            try:
                tx(OTC, CANCEL + f"{oid:064x}", f"撤买单 #{oid}")
            except Exception as e:
                print(f"[实盘] 撤单 #{oid} 没成功：{str(e)[:120]}")

    def halt(why, mine):
        print(f"::error::[实盘] ⛔ 停机：{why}")
        cancel_all(mine)
        st["halted"] = {"t": now, "why": why}

    mine, others_bid, others_ask = my_orders(now)
    if st.get("halted"):
        if mine:
            cancel_all(mine)
        print(f"[实盘] 停机中（{st['halted'].get('why')}）。看过没问题后，把仓库变量 BID_RESET 改成一个新值再开")
        return
    if mine:  # 有单挂着：手里的币最贵可能是按这张单的价买的
        st["cost_px"] = max(st.get("cost_px") or 0, max(m[1] for m in mine))

    r_tok, r_dai = reserves()
    # ① 卖币
    ail = bal(AIL, ME)
    if ail >= MIN_SELL:
        coins = ail / 1e18
        cost_px = st.get("cost_px") or st.get("live_bid") or 0
        expect = dex_net(coins, r_tok, r_dai)
        # cost_px = 0：钱包里本来就有的零头（不是买单接来的），按市价卖掉，顺便实测一次到手率
        if cost_px and expect < coins * cost_px:
            return halt(f"手里 {coins:.4f} 枚，现在去 DEX 卖只得 {expect:.2f} DAI，低于成本 {coins * cost_px:.2f}（每枚 {cost_px:.2f}）。没卖，币留在钱包", mine)
        min_out = max(int(expect * 0.997 * 1e18), int(coins * cost_px * 1e18) + 1)  # 最低到手：不低于成本
        try:
            if int(call(AIL, "0xdd62ed3e" + pad(ME) + pad(ROUTER)), 16) < ail:
                tx(AIL, "0x095ea7b3" + pad(ROUTER) + f"{MAX_U:064x}", "授权官方路由卖 AiLGNS（只这一次）")
            d0 = bal(DAI, ME)
            data = SWAP_FOT + f"{ail:064x}{min_out:064x}{160:064x}" + pad(ME) + f"{now + 600:064x}" + f"{2:064x}" + pad(AIL) + pad(DAI)
            tx(ROUTER, data, f"DEX 卖出 {coins:.4f} AiLGNS")
            back = (bal(DAI, ME) - d0) / 1e18
        except Exception as e:
            return halt(f"DEX 卖出没成功（{str(e)[:160]}）。币还在钱包里", mine)
        pnl = back - coins * cost_px
        st["real_coins"] = round(st.get("real_coins", 0) + coins, 6)
        st["real_pnl"] = round(st.get("real_pnl", 0) + pnl, 4)
        st["real_fee"] = round(st.get("real_fee", 0) + coins * cost_px * OTC_FEE, 4)
        st["trades"] = (st.get("trades", []) + [{"t": now, "coins": round(coins, 6), "cost": round(cost_px, 3), "back": round(back, 3), "pnl": round(pnl, 3)}])[-200:]
        print(f"[实盘] 卖出 {coins:.4f} 枚：成本每枚 {cost_px:.2f}，到手 {back:.2f} DAI，这头 {pnl:+.2f} DAI（实际到手率 {back / (coins * r_dai / r_tok):.4f}）")
        st["cost_px"] = 0
        rate = back / (coins * r_dai / r_tok)
        st["sell_rate"] = round(rate, 4)
        if rate < 0.962:
            return halt(f"DEX 实际到手率 {rate:.4f}，比预期的 0.967 低，先停下来查原因", mine)
        r_tok, r_dai = reserves()
        mine, others_bid, others_ask = my_orders(now)
        if mine:
            st["cost_px"] = max(m[1] for m in mine)

    # ② 总资产 / 回撤
    dai = bal(DAI, ME)
    locked = sum(m[2] for m in mine)
    left_ail = bal(AIL, ME) / 1e18
    equity = (dai + locked) / 1e18 + (dex_net(left_ail, r_tok, r_dai) if left_ail > 0 else 0)
    # 2026-11-17 误停机的教训：官方节点后面有好几台机器，偶尔有一台慢半拍。刚卖完币（DAI 已到账）那一下如果读到的还是「成交前」的挂单余额，
    # 同一笔钱就被数了两遍，最高点虚高 166 DAI，下一轮就被当成「亏了 166」停机。所以最高点和停机都要连续两次读数一致才算数
    prev = st.get("equity")
    st["equity"] = round(equity, 3)
    st["peak"] = max(st.get("peak") or 0, min(equity, prev) if prev else equity)
    if equity < st["peak"] - MAX_LOSS and prev is not None and prev < st["peak"] - MAX_LOSS:
        return halt(f"总资产 {equity:.2f} DAI，比最高点 {st['peak']:.2f} 少了超过 {MAX_LOSS:g} DAI（如果是你自己把钱转走了，改 BID_RESET 就行）", mine)

    # ③ 挂单。站长 2026-11-17：钱包里的 DAI 全部拿去挂（留 2 DAI 付 Gas），最多 SIZE 枚；出价按「这张单真被一次吃完、一次卖进 DEX」的到手价算
    can = max(0, dai + locked - GAS_KEEP) / 1e18
    coins = max(0.01, min(SIZE, can / (r_dai / r_tok * (1 - SELL_TAX))))
    net = dex_net(coins, r_tok, r_dai) / coins
    bid = int(net * (1 - MARGIN) * 100) / 100
    if others_ask and bid >= others_ask:
        bid = int(round((others_ask - 0.01) * 100)) / 100
    budget = min(int(SIZE * bid * 1e18), dai + locked - GAS_KEEP)
    good = [m for m in mine if m[3] - now > 600 and abs(m[1] - bid) / bid <= 0.0005 and m[1] <= net * (1 - MARGIN / 2) and m[2] >= budget * 98 // 100]  # 站长 2026-11-17：被吃掉一点就马上补回满额（原来少一半才补）；留 2% 是免得为几分钱零头来回撤挂
    if len(mine) == 1 and good:
        print(f"[实盘] 买单 #{mine[0][0]} 还合适：挂价 {mine[0][1]:.2f}（该挂 {bid:.2f}），剩 {mine[0][2] / 1e18:.2f} DAI · 总资产 {equity:.2f}")
        st["live_bid"] = mine[0][1]
        return
    if mine:
        cancel_all(mine)
        if my_orders(now)[0]:
            time.sleep(2)  # 节点可能慢半拍，等一下再读一次
            if my_orders(now)[0]:
                return halt("旧买单撤不掉", [])
        dai = bal(DAI, ME)
        budget = min(int(SIZE * bid * 1e18), dai - GAS_KEEP)
    if budget < MIN_ORDER:
        print(f"[实盘] 钱包只有 {dai / 1e18:.2f} DAI，不够挂单（至少 {MIN_ORDER / 1e18 + GAS_KEEP / 1e18:.0f}）。往 {ME} 转 DAI 就会自动开始")
        st["live_bid"] = 0
        return
    want = -(-budget * 100 // int(round(bid * 100)))  # 往上取：实际单价只会比 bid 低一丁点
    try:
        if int(call(DAI, "0xdd62ed3e" + pad(ME) + pad(OTC)), 16) < budget:
            tx(DAI, "0x095ea7b3" + pad(OTC) + f"{MAX_U:064x}", "授权 OTC 合约用 DAI 挂单（只这一次）")
        tx(OTC, CREATE + f"{0:064x}{budget:064x}{want:064x}{DURATION:064x}", f"挂买单 {budget / 1e18:.2f} DAI @ {bid:.2f}")
    except Exception as e:
        return halt(f"挂单没成功（{str(e)[:160]}）", [])
    st["live_bid"] = bid
    st["cost_px"] = max(st.get("cost_px") or 0, bid)
    print(f"[实盘] DEX 每枚到手 {net:.2f} · 买单挂 {bid:.2f} · 别人的买一 {others_bid:.2f} / 卖一 {others_ask:.2f} · 总资产 {equity:.2f} DAI"
          f" · 累计实盘 {st.get('real_coins', 0):.4f} 枚 这头 {st.get('real_pnl', 0):+.2f} DAI")


def run_live():
    st = load()
    try:
        live(st)
    finally:
        st["live_at"] = int(time.time())
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)


if __name__ == "__main__":
    main()                      # 空跑账照记（只读）
    if os.environ.get("BID_ON") == "1":
        run_live()
