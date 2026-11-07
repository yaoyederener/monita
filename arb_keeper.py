"""⚡ EVM 套利做市商（内部版，2026-11-05 站长定）：AiLGNS 在本站 OTC 和官方 DEX 之间的无风险差价套利。

只用自己的套利钱包（ARB_KEY，地址 0x06cc…4437），不碰任何合约的管理功能，和普通客户一样吃单 / 换币：
  路径 A（OTC 有人贱卖）：OTC 卖单价 低于 DEX 卖出到手价 → 在 OTC 吃掉卖单（拿到 99%）→ 马上在 DEX 卖掉 → 差价落袋
  路径 B（OTC 有人高价求购）：OTC 买单价 ×99% 高于 DEX 买入成本 → 在 DEX 买币 → 马上卖进 OTC 买单 → 差价落袋
  （同一盘口里买单 ≥ 卖单的交叉机会，由 otc_matcher.py + 撮合合约处理，这里不重复做）
每次只做一小笔（≤ MAX_DAI），两条腿几秒内完成，不持仓过夜；算出来的净利润 ≥ MIN_PROFIT 且 ≥ MIN_BPS 才出手。
DEX 直接走交易对合约（UniswapV2 式 swap），买卖的实际折损率用真实成交校准后存在 data/arb.json，第一次用保守默认值。
    python arb_keeper.py            # 检查并执行
    python arb_keeper.py --dry-run  # 只算不发
"""
import argparse
import json
import os
import time

from eth_account import Account

from chains import rpc, send_tx
import order_age
from otc_buyback import AIL, AIL_OTC, AIL_PAIR, DAI, num, orders, pad, sel, spot, take

ARB_ADDR = "0x06cc2a46d7ad17496c2de9c61362808e06b44437"
MAX_DAI = int(float(os.environ.get("ARB_MAX_DAI", "300")) * 1e18)  # 单笔最多动用多少 DAI
MIN_PROFIT = int(float(os.environ.get("ARB_MIN_PROFIT", "1")) * 1e18)
PATH_B = os.environ.get("ARB_PATH_B", "0") == "1"  # 路径 B（DEX 买 → OTC 卖）：2026-11-07 实测普通钱包直接在交易对买 AiLGNS 会 TRANSFER_FAILED，默认关；只有 probe 证明买得通才开  # 单笔净利至少 1 DAI（站长 2026-11-08 定：大于 1u 就做，每笔必须有利润）
MIN_BPS = int(os.environ.get("ARB_MIN_BPS", "30"))  # 且至少 0.3%
GAS_KEEP = 2 * 10**18  # 钱包永远留 2 DAI 付 Gas（Anubis 链 Gas 就是 DAI）
FEE = 99  # OTC 吃单到手 99%
SAFETY = 0.004  # DEX 成交量下限再留 0.4% 余量，避免因价格微动而回滚
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "arb.json")
TX = "https://anubisscan.io/tx/"
MAX_U = (1 << 256) - 1


def load():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"sell_eff": 0.965, "buy_eff": 0.995, "trades": [], "pnl": 0.0, "probes": 0}


def save(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(st, f, ensure_ascii=False)


# UniswapV2：x*y=k，0.3% 手续费；AiLGNS 进池子有税，用 sell_eff / buy_eff（实测校准）打折
def dex_out(amount_in, r_in, r_out):
    a = amount_in * 997
    return a * r_out // (r_in * 1000 + a)


def dex_in(amount_out, r_in, r_out):
    return (r_in * amount_out * 1000) // ((r_out - amount_out) * 997) + 1


def bal(tok, who):
    return num(tok, "0x70a08231" + pad(who))


def probe(acct, me, st):
    """实测：用 1 DAI 直接在交易对上买 AiLGNS（不走官方 router），再原样卖回去。验证普通钱包能不能直接买 / 卖，并校准折损率"""
    dai0, ail0 = bal(DAI, me), bal(AIL, me)
    r_tok, r_dai = spot(AIL_PAIR)
    put = 10**18
    want = dex_out(put, r_dai, r_tok) * 95 // 100  # 买：DAI 进 → 币出，留 5% 余量
    print(f"[probe] 买：1 DAI 换 ≥ {want / 1e18:.6f} AiLGNS（理论 {dex_out(put, r_dai, r_tok) / 1e18:.6f}）")
    send_tx("anubis", acct, DAI, "0xa9059cbb" + pad(AIL_PAIR) + f"{put:064x}", "probe 转 1 DAI 进交易对")
    try:
        h1 = send_tx("anubis", acct, AIL_PAIR, sel("swap(uint256,uint256,address,bytes)") + f"{want:064x}" + f"{0:064x}" + pad(me) + f"{128:064x}" + f"{0:064x}", "probe DEX 买入")
    except Exception as e:
        print(f"[probe] ❌ 直接买入失败（{str(e)[:160]}）。1 DAI 留在交易对里了（会被下一笔 swap 吃掉，当学费）。结论：普通钱包不能直接在 DEX 买 AiLGNS，只能走「OTC 低价吃 → DEX 卖」一条腿")
        st["probe"] = {"t": int(time.time()), "buy": False, "err": str(e)[:200]}
        save(st)
        return
    got = bal(AIL, me) - ail0
    eff_buy = (dex_out(put, r_dai, r_tok) / got) if got else 0
    print(f"[probe] ✅ 直接买入成功：到手 {got / 1e18:.6f} AiLGNS（理论/实收 = {eff_buy:.4f}） {TX}{h1}")
    r_tok, r_dai = spot(AIL_PAIR)
    out_min = dex_out(got, r_tok, r_dai) * 90 // 100  # 卖：币进 → DAI 出，留 10% 余量（有税）
    send_tx("anubis", acct, AIL, "0xa9059cbb" + pad(AIL_PAIR) + f"{got:064x}", "probe 转币进交易对")
    try:
        h2 = send_tx("anubis", acct, AIL_PAIR, sel("swap(uint256,uint256,address,bytes)") + f"{0:064x}" + f"{out_min:064x}" + pad(me) + f"{128:064x}" + f"{0:064x}", "probe DEX 卖出")
    except Exception as e:
        print(f"[probe] ❌ 直接卖出失败（{str(e)[:160]}）。币留在交易对里了。结论：直接买能，直接卖不能")
        st["probe"] = {"t": int(time.time()), "buy": True, "sell": False, "err": str(e)[:200]}
        save(st)
        return
    back = bal(DAI, me) - (dai0 - put)
    theo = dex_out(got, r_tok, r_dai)
    eff_sell = back / theo if theo else 0
    st["probe"] = {"t": int(time.time()), "buy": True, "sell": True, "got": got / 1e18, "back": back / 1e18, "buy_eff": round(1 / eff_buy, 4) if eff_buy else None, "sell_eff": round(eff_sell, 4)}
    if 0.5 < eff_sell < 1.01: st["sell_eff"] = round(eff_sell, 4)
    if eff_buy and 0.9 < 1 / eff_buy < 1.01: st["buy_eff"] = round(1 / eff_buy, 4)
    save(st)
    print(f"[probe] ✅ 直接卖出成功：1 DAI 买的币卖回 {back / 1e18:.4f} DAI（卖出折损 {(1 - eff_sell) * 100:.2f}%，买入折损 {(eff_buy - 1) * 100:.2f}%）。两条腿都通。 {TX}{h2}")


def probe_sell(acct, me, st):
    """只测卖出：钱包里已有的 AiLGNS（最多 0.002 个）直接转进交易对 swap 成 DAI，验证「OTC 低价吃 → DEX 卖」这条腿通不通"""
    ail = bal(AIL, me)
    if ail < 10**14: raise SystemExit("套利钱包没有 AiLGNS，先转 0.002 个进来再测卖出")
    put = min(ail, 2 * 10**15)
    dai0 = bal(DAI, me)
    r_tok, r_dai = spot(AIL_PAIR)
    theo = dex_out(put, r_tok, r_dai)
    out_min = theo * 85 // 100
    print(f"[probe] 卖：{put / 1e18:.6f} AiLGNS 理论换 {theo / 1e18:.4f} DAI")
    send_tx("anubis", acct, AIL, "0xa9059cbb" + pad(AIL_PAIR) + f"{put:064x}", "probe 转币进交易对")
    try:
        h2 = send_tx("anubis", acct, AIL_PAIR, sel("swap(uint256,uint256,address,bytes)") + f"{0:064x}" + f"{out_min:064x}" + pad(me) + f"{128:064x}" + f"{0:064x}", "probe DEX 卖出")
    except Exception as e:
        print(f"[probe] ❌ 直接卖出失败（{str(e)[:160]}）。结论：普通钱包直接买、直接卖都不行，DEX 套利做不了")
        st["probe"] = {"t": int(time.time()), "buy": False, "sell": False, "err": str(e)[:200]}
        save(st)
        return
    back = bal(DAI, me) - dai0
    eff_sell = back / theo if theo else 0
    st["probe"] = {"t": int(time.time()), "buy": False, "sell": True, "sold": put / 1e18, "back": back / 1e18, "sell_eff": round(eff_sell, 4)}
    if 0.5 < eff_sell < 1.01: st["sell_eff"] = round(eff_sell, 4)
    save(st)
    print(f"[probe] ✅ 直接卖出成功：{put / 1e18:.6f} AiLGNS 卖得 {back / 1e18:.4f} DAI（折损 {(1 - eff_sell) * 100:.2f}%）。路径 A（OTC 低价吃 → DEX 卖）可以开。 {TX}{h2}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--probe", action="store_true", help="用 1 DAI 实测直接买卖是否可行，并校准折损率")
    ap.add_argument("--probe-sell", action="store_true", help="只测卖出：用钱包里已有的 ≤0.002 AiLGNS 直接卖进交易对")
    args = ap.parse_args()
    dry = args.dry_run
    if args.probe_sell:
        key = os.environ.get("ARB_KEY")
        if not key: raise SystemExit("probe 需要 ARB_KEY")
        acct = Account.from_key(key)
        probe_sell(acct, acct.address.lower(), load())
        return
    if args.probe:
        key = os.environ.get("ARB_KEY")
        if not key: raise SystemExit("probe 需要 ARB_KEY")
        acct = Account.from_key(key)
        st = load()
        if bal(DAI, acct.address.lower()) < 3 * 10**18: raise SystemExit("套利钱包 DAI 不足 3，先转 20 DAI 进来")
        probe(acct, acct.address.lower(), st)
        return
    key = os.environ.get("ARB_KEY")
    acct = Account.from_key(key) if key else None
    me = acct.address.lower() if acct else ARB_ADDR
    st = load()
    dai, ail = bal(DAI, me), bal(AIL, me)
    r_tok, r_dai = spot(AIL_PAIR)  # (币, DAI)
    dex_px = r_dai / r_tok
    print(f"套利钱包 {me}：{dai / 1e18:.2f} DAI + {ail / 1e18:.6f} AiLGNS · DEX 现价 {dex_px:.3f} · 校准 卖出 {st['sell_eff']:.4f} 买入 {st['buy_eff']:.4f}")
    budget = min(MAX_DAI, dai - GAS_KEEP)
    if budget < 10**18:
        print("本金不足（留 2 DAI 当 Gas），不操作")
        return
    now = time.time() + 60
    raw = orders(AIL_OTC)
    seen = order_age.note(AIL_OTC, raw, now)  # 🕐 让客户先吃：挂出不到 2 分钟的单不碰
    book = [o for o in raw if o[3] and o[4] > now and o[7] > 0 and o[1] != me and not order_age.fresh(seen, o[0], now)]
    asks = sorted([o for o in book if o[2]], key=lambda o: o[6] / o[5])  # 卖 AiLGNS：give=币 want=DAI
    bids = sorted([o for o in book if not o[2]], key=lambda o: -(o[5] / o[6]))  # 买 AiLGNS：give=DAI want=币
    best = None

    # 路径 A：OTC 买（付 DAI，到手 99% 币）→ DEX 卖
    for o in asks:
        amount, pay = take(o, budget)  # amount = 拿到的币（扣费前），pay = 付的 DAI
        if amount == 0:
            continue
        got = amount * FEE // 100
        out = int(dex_out(got, r_tok, r_dai) * st["sell_eff"])
        profit = out - pay
        if profit > MIN_PROFIT and profit * 10000 > pay * MIN_BPS and (best is None or profit > best["profit"]):
            best = {"path": "A", "o": o, "amount": amount, "pay": pay, "got": got, "out": out, "profit": profit}
    # 路径 B：DEX 买币 → 卖进 OTC 买单（到手 99% DAI）
    for o in (bids if PATH_B else []):
        price = o[5] / o[6]  # 每币多少 DAI
        # 这张买单最多还能吃多少 DAI：左手是对方剩的 DAI(giveLeft)，要付的币 = pay
        max_dai = min(o[7], budget * 2)
        amount, pay = take(o, (max_dai * o[6]) // o[5] + 1)  # amount = 拿到的 DAI，pay = 付的币
        if amount == 0:
            continue
        cost = int(dex_in(pay, r_dai, r_tok) / st["buy_eff"]) + 1  # 在 DEX 买 pay 个币要花多少 DAI
        if cost > budget:
            continue
        profit = amount * FEE // 100 - cost
        if profit > MIN_PROFIT and profit * 10000 > cost * MIN_BPS and (best is None or profit > best["profit"]):
            best = {"path": "B", "o": o, "amount": amount, "pay": pay, "cost": cost, "profit": profit}
    if not best:
        a0 = asks[0][6] / asks[0][5] if asks else 0
        b0 = bids[0][5] / bids[0][6] if bids else 0
        print(f"没有套利机会：OTC 卖一 {a0:.3f} / 买一 {b0:.3f}，DEX {dex_px:.3f}（卖一需 < {dex_px * st['sell_eff'] * 0.99:.3f}，买一需 > {dex_px / st['buy_eff'] / 0.99:.3f}）")
        return
    o = best["o"]
    if dry or not acct:
        print(f"[dry] 路径 {best['path']} 单 #{o[0]} 预计净赚 {best['profit'] / 1e18:.4f} DAI")
        return

    def approve(tok, spender):
        if num(tok, "0xdd62ed3e" + pad(me) + pad(spender)) < 10**30:
            send_tx("anubis", acct, tok, "0x095ea7b3" + pad(spender) + f"{MAX_U:064x}", "授权")

    dai0, ail0 = dai, ail
    txs = []
    if best["path"] == "A":
        approve(DAI, AIL_OTC)
        txs.append(send_tx("anubis", acct, AIL_OTC, sel("fill(uint256,uint256,uint256)") + f"{o[0]:064x}" + f"{best['amount']:064x}" + f"{best['pay']:064x}", "OTC 吃卖单"))
        got = bal(AIL, me) - ail0
        if got <= 0:
            raise RuntimeError("吃单后没收到币")
        # 卖进 DEX：先把币转进交易对，再 swap 取 DAI（token0 = 币，token1 = DAI）
        r_tok, r_dai = spot(AIL_PAIR)
        want_out = int(dex_out(got, r_tok, r_dai) * st["sell_eff"] * (1 - SAFETY))
        floor_out = best["pay"] + MIN_PROFIT  # 每笔必须有利润：到手 DAI 至少 = 成本 + 1 DAI，否则不卖
        if want_out < floor_out:
            # 吃单到卖出之间 DEX 价动了：不亏卖。把币挂回 OTC，要价 = 成本 + 1.5%，等人来买（这是库存，不是亏损）
            ask_dai = (best["pay"] * 1015 // 1000) + MIN_PROFIT
            approve(AIL, AIL_OTC)
            h = send_tx("anubis", acct, AIL_OTC, sel("create(uint256,uint256,uint256,uint256)") + f"{1:064x}" + f"{got:064x}" + f"{ask_dai:064x}" + f"{7 * 86400:064x}", "DEX 价变了，挂回 OTC")
            st["held"] = st.get("held", []) + [{"t": int(time.time()), "coin": got / 1e18, "cost": best["pay"] / 1e18, "ask": ask_dai / 1e18, "tx": h}]
            save(st)
            print(f"⚠️ 吃完单 DEX 只能卖 {want_out / 1e18:.2f}（成本 {best['pay'] / 1e18:.2f}），不够 1 DAI 利润：已把 {got / 1e18:.6f} 币按 {ask_dai / 1e18:.2f} DAI 挂回 OTC，不亏 {TX}{h}")
            return
        want_out = max(want_out, floor_out)  # swap 的最低到手量按利润线卡死，链上达不到就整笔回滚
        send_tx("anubis", acct, AIL, "0xa9059cbb" + pad(AIL_PAIR) + f"{got:064x}", "转币进 DEX")
        txs.append(send_tx("anubis", acct, AIL_PAIR, sel("swap(uint256,uint256,address,bytes)") + f"{0:064x}" + f"{want_out:064x}" + pad(me) + f"{128:064x}" + f"{0:064x}", "DEX 卖出"))
        real = (bal(DAI, me) - dai0)  # 净 DAI 变化 = 利润（已含 Gas）
        theo = dex_out(got, r_tok, r_dai)
        if theo > 0:
            st["sell_eff"] = round(0.7 * st["sell_eff"] + 0.3 * ((real + best["pay"]) / theo), 4)
    else:
        # DEX 买币：把 DAI 转进交易对，swap 取币；再把币卖进 OTC 买单
        r_tok, r_dai = spot(AIL_PAIR)
        need = best["pay"]
        cost = int(dex_in(need, r_dai, r_tok) / st["buy_eff"] * (1 + SAFETY)) + 1
        if best["amount"] * FEE // 100 - cost < MIN_PROFIT:  # 算到买之前 DEX 涨了，利润不够 1 DAI：不买，什么都没发生
            print(f"⚠️ 下手前 DEX 涨了，利润不足 1 DAI，放弃（成本 {cost / 1e18:.2f} / 到手 {best['amount'] * FEE // 100 / 1e18:.2f}）")
            return
        send_tx("anubis", acct, DAI, "0xa9059cbb" + pad(AIL_PAIR) + f"{cost:064x}", "转 DAI 进 DEX")
        txs.append(send_tx("anubis", acct, AIL_PAIR, sel("swap(uint256,uint256,address,bytes)") + f"{need:064x}" + f"{0:064x}" + pad(me) + f"{128:064x}" + f"{0:064x}", "DEX 买入"))
        got = bal(AIL, me) - ail0
        if got < need:
            raise RuntimeError(f"DEX 买到的币不够：要 {need} 实收 {got}")
        approve(AIL, AIL_OTC)
        txs.append(send_tx("anubis", acct, AIL_OTC, sel("fill(uint256,uint256,uint256)") + f"{o[0]:064x}" + f"{best['amount']:064x}" + f"{best['pay']:064x}", "OTC 吃买单"))
        real = bal(DAI, me) - dai0
        theo = dex_in(need, r_dai, r_tok)
        if theo > 0:
            st["buy_eff"] = round(0.7 * st["buy_eff"] + 0.3 * (theo / cost), 4)
    st["pnl"] = round(st.get("pnl", 0) + real / 1e18, 6)
    st["trades"] = (st.get("trades") or [])[-300:] + [{"t": int(time.time()), "path": best["path"], "id": o[0], "size": round((best["pay"] if best["path"] == "A" else best["cost"]) / 1e18, 2), "profit": round(real / 1e18, 6), "dex": round(dex_px, 3), "tx": txs}]
    save(st)
    print(f"✅ 路径 {best['path']} 单 #{o[0]} 净赚 {real / 1e18:.4f} DAI（预计 {best['profit'] / 1e18:.4f}） " + " ".join(TX + h for h in txs))


if __name__ == "__main__":
    main()
