"""收入回购分账：收款钱包收到的 Anubis LGNS / DAI 换成 AiLGNS，连同收到的 AiLGNS 按 40% / 30% / 30% 分出去。

回购分账合约 AilgnsBuyback（Anubis 链，地址、比例、价格保护写死）：
  LGNS 卖进 LGNS OTC 的买单（出价不低于 DEX 现价 50%，不走 DEX，DEX 卖出要扣约 50%）
  → DAI 扫 AiLGNS OTC 的卖单（要价不高于 DEX 现价 115%）
  → AiLGNS 40% 给 0xD53A…7285，30% 给 0xe878…91c3，30% 给 0x5505…B322。
攒够 MIN_USD 美金才换一次；收款钱包里永远留 DAI_RESERVE 的 DAI 付 Gas；触发钱包 DAI 不够时每次从收入里补 0.5 DAI。
Polygon 链的收入先攒在收款钱包里，不换、不分。
收款钱包需要先在 ailgns.dev/#/otc/split 授权回购合约（LGNS、DAI、AiLGNS 各一次）。
需要的环境变量：OTC_KEEPER_KEY（部署回购合约的触发钱包）
    python otc_buyback.py            # 检查并回购分账
    python otc_buyback.py --dry-run  # 只检查、算一遍，不发交易
"""
import argparse
import json
import os
import time

from eth_account import Account
from eth_utils import keccak

from chains import rpc, send_tx
from notify_mail import send as send_mail

BUYBACK = "0x99f47ca8e7ed873e01a337aefc5b75f5328d2da3"  # 回购分账合约地址（2026-11-03 部署）
START = 18104818  # 回购合约部署所在区块：从这里开始算收入
SOURCE = "0xedb9c6900a11d2dd37da6b59f3604c35a39db1e6"
MATCHER = "0xdcc5354175d60ffc8364e71e792fcd66366f00fe"  # OTC 撮合合约：退回收款钱包的是本金，不算收入（利润直接进回购合约）
LGNS, DAI, AIL = "0x4d1d808a081fdac440703b3765fc61f8028c06b8", "0x83fd06f0846d9d90b3016bf670efe2e0b11cde14", "0x256edb652db43e721ec6a12b6b51b6967d3bb8cb"
LGNS_OTC, AIL_OTC = "0xb6a7a3be9a17ad241665ab9bddbd9a573d488818", "0x4e730be6d46be71223ded5a32dc45799f6d4bc15"
LGNS_PAIR, AIL_PAIR = "0x32a4586797e3f561f41c0f47ca57ed08d64f3dc0", "0x0a1cce65d995c6c5100e6f5ba3c388ef34cecaf0"  # token0 = 币，token1 = DAI
TOKENS = [("LGNS", LGNS, 9), ("DAI", DAI, 18), ("AiLGNS", AIL, 18)]
MIN_USD = 300  # 攒够这么多美金（按 OTC 实际能成交的价格算）才换一次
DAI_RESERVE = 5 * 10**18  # 收款钱包永远留 5 DAI 付 Gas
GAS_TIP, KEEPER_LOW = 5 * 10**17, 2 * 10**18  # 触发钱包 DAI 低于 2 时，从收入里补 0.5
MIN_BID, MAX_ASK = 0.5, 1.15  # 和合约里一样的价格保护（这里先筛一遍，合约里再查一遍）
MAX_BIDS, MAX_ASKS = 8, 12  # 合约一次最多吃 20 张单
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "buyback.json")
TX = "https://anubisscan.io/tx/"


def sel(sig):
    return "0x" + keccak(text=sig).hex()[:8]


def pad(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def call(to, data):
    return rpc("anubis", "eth_call", [{"to": to, "data": data}, "latest"])


def num(to, data):
    return int(call(to, data), 16)


def spot(pair):
    w = call(pair, "0x0902f1ac")[2:]
    return int(w[:64], 16), int(w[64:128], 16)  # (币, DAI)


def orders(otc):
    """读最近 1000 张挂单：[(id, maker, 卖币?, open, expiry, giveTotal, wantTotal, giveLeft)]"""
    n = num(otc, sel("ordersCount()"))
    frm = max(0, n - 1000)
    h = call(otc, sel("getOrders(uint256,uint256)") + f"{frm:064x}" + f"{n - frm:064x}")[2:]
    w = [h[i:i + 64] for i in range(0, len(h), 64)]
    out = []
    for i in range(int(w[1], 16)):
        o = w[2 + i * 7:9 + i * 7]
        out.append((frm + i, "0x" + o[0][24:], int(o[1], 16) == 1, int(o[2], 16) == 1, int(o[3], 16), int(o[4], 16), int(o[5], 16), int(o[6], 16)))
    return out


def take(o, budget):
    """和合约 _take 一样：最多用 budget 个对方要的币，返回 (拿到多少对方的币, 付多少)"""
    give, want, left = o[5], o[6], o[7]
    full = (left * want + give - 1) // give
    if budget >= full:
        return left, full
    amount = budget * give // want
    if amount == 0:
        return 0, 0
    pay = (amount * want + give - 1) // give
    if pay > budget:
        amount -= 1
        pay = (amount * want + give - 1) // give
    return amount, pay


def plan(book, budget, want_sell, ok, limit):
    """按价格从优到差挑单：返回 (单号列表, 付掉的, 拿到的扣 1% 前)"""
    now = time.time() + 120
    live = [o for o in book if o[2] == want_sell and o[3] and o[4] > now and o[7] > 0 and o[1] != BUYBACK and ok(o)]
    live.sort(key=lambda o: o[6] / o[5] if want_sell else -o[5] / o[6])  # 卖单按要价从低到高，买单按出价从高到低
    ids, paid, got = [], 0, 0
    for o in live[:limit]:
        if budget - paid <= 0:
            break
        a, p = take(o, budget - paid)
        if a:
            ids.append(o[0])
            paid += p
            got += a
    return ids, paid, got


def income(state, latest):
    """从 START 起转进收款钱包的 LGNS / DAI / AiLGNS（不算回购合约退回来的、自己转自己的）"""
    for name, tok, dec in TOKENS:
        st = state.setdefault(tok, {"scanned_to": START - 1, "income": "0"})
        inc = int(st["income"])
        for b in range(st["scanned_to"] + 1, latest + 1, 5000):
            end = min(latest, b + 4999)
            logs = rpc("anubis", "eth_getLogs", [{"address": tok, "fromBlock": hex(b), "toBlock": hex(end), "topics": [TRANSFER, None, "0x" + pad(SOURCE)]}])
            inc += sum(int(l["data"], 16) for l in logs if "0x" + l["topics"][1][26:] not in (SOURCE, BUYBACK, MATCHER))
            st["scanned_to"] = end
        st["income"] = str(inc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not BUYBACK:
        print("回购合约还没部署（BUYBACK 空着），跳过")
        return
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    latest = int(rpc("anubis", "eth_blockNumber", []), 16)
    income(state, latest)

    # 这次能取多少：待处理收入、收款钱包余额（DAI 留 5 个）、授权，三者取小
    take_in = {}
    for name, tok, dec in TOKENS:
        pending = int(state[tok]["income"]) - num(BUYBACK, sel("totalPulled(address)") + pad(tok))
        bal = num(tok, "0x70a08231" + pad(SOURCE)) - (DAI_RESERVE if tok == DAI else 0)
        allow = num(tok, "0xdd62ed3e" + pad(SOURCE) + pad(BUYBACK))
        take_in[tok] = max(0, min(pending, bal, allow))
        print(f"{name}：待处理 {pending / 10 ** dec:.6f}，收款钱包可用 {max(bal, 0) / 10 ** dec:.6f}，授权 {allow / 10 ** dec:.4g}，这次取 {take_in[tok] / 10 ** dec:.6f}"
              + ("　← 授权不够，请到 ailgns.dev/#/otc/split 授权回购合约" if allow < pending else ""))
    carry = {tok: num(tok, "0x70a08231" + pad(BUYBACK)) for _, tok, _ in TOKENS}  # 上次没换完、留在合约里的

    # 按 OTC 上现在能成交的价格估一遍
    lt, ld = spot(LGNS_PAIR)
    at, ad = spot(AIL_PAIR)
    lgns_all = take_in[LGNS] + carry[LGNS]
    bids, lgns_sold, dai_got = plan(orders(LGNS_OTC), lgns_all, False, lambda o: o[5] * lt >= o[6] * ld * MIN_BID, MAX_BIDS)
    dai_from_lgns = dai_got - dai_got // 100
    keeper = Account.from_key(os.environ["OTC_KEEPER_KEY"]) if os.environ.get("OTC_KEEPER_KEY") else None
    tip = GAS_TIP if keeper and num(DAI, "0x70a08231" + pad(keeper.address)) < KEEPER_LOW else 0
    dai_all = take_in[DAI] + carry[DAI] + dai_from_lgns - tip
    asks, dai_spent, ail_got = plan(orders(AIL_OTC), max(dai_all, 0), True, lambda o: o[6] * at <= o[5] * ad * MAX_ASK, MAX_ASKS)
    bought = ail_got - ail_got // 100
    ail_px = ad / at
    usd = (dai_from_lgns + take_in[DAI] + carry[DAI]) / 1e18 + (take_in[AIL] + carry[AIL]) / 1e18 * ail_px
    print(f"按现在的挂单：卖 {lgns_sold / 1e9:,.4f} LGNS（{len(bids)} 张买单）→ {dai_from_lgns / 1e18:,.2f} DAI；"
          f"花 {dai_spent / 1e18:,.2f} DAI（{len(asks)} 张卖单）→ {bought / 1e18:.6f} AiLGNS；这批合计约 {usd:,.2f} 美金")
    if usd < MIN_USD:
        print(f"不到 {MIN_USD} 美金，攒着下次再换")
        _save(state, args.dry_run)
        return
    if args.dry_run or not keeper:
        print("dry-run / 没设置 OTC_KEEPER_KEY：不发交易")
        _save(state, args.dry_run)
        return

    ids_arr = lambda ids: f"{len(ids):064x}" + "".join(f"{i:064x}" for i in ids)
    head = 7 * 32
    off_bids = head
    off_asks = head + 32 * (1 + len(bids))
    data = (sel("run(uint256,uint256[],uint256,uint256[],uint256,uint256,uint256)") + f"{take_in[LGNS]:064x}" + f"{off_bids:064x}" + f"{take_in[DAI]:064x}"
            + f"{off_asks:064x}" + f"{take_in[AIL]:064x}" + f"{bought * 97 // 100:064x}" + f"{tip:064x}" + ids_arr(bids) + ids_arr(asks))
    try:
        h = send_tx("anubis", keeper, BUYBACK, data, "回购分账")
    except Exception as e:  # 挂单刚被别人吃掉等：下次再试
        print(f"回购分账失败，下次再试：{e}")
        _save(state, False)
        return
    total = (bought + take_in[AIL] + carry[AIL]) / 1e18
    msg = (f"本次回购：卖 {lgns_sold / 1e9:,.4f} LGNS、花 {dai_spent / 1e18:,.2f} DAI，买到约 {bought / 1e18:.6f} AiLGNS。\n"
           f"连同收到的 AiLGNS 共分出约 {total:.6f} AiLGNS：40% 约 {total * 0.4:.6f}，30% 每份约 {total * 0.3:.6f}。\n{TX}{h}")
    print(msg)
    ops = [e.strip() for e in (os.environ.get("OPS_EMAILS") or "").split(",") if e.strip()]
    send_mail("AiLGNS 收入回购分账", msg + "\n\n—— AiLGNS 服务站", ops or None)
    _save(state, False)


def _save(state, dry_run):
    if dry_run:
        return
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(state, f)


if __name__ == "__main__":
    main()
