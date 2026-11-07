"""OTC 自动撮合：同一个 OTC 市场里，卖单要价比买单出价低（差价够付两边 1% 手续费 + Gas）时，
调用撮合合约 AilgnsOtcMatcher.matchOrders：从收款钱包临时取 DAI，吃卖单 → 卖给买单 → 循环，
本金同一笔交易里原样退回收款钱包，利润进回购分账合约（攒够 300 美金自动分）。赚不到钱合约整笔回滚。
每次动用收款钱包能用的最多 DAI（余额 - 留 5 DAI 付 Gas，且不超过授权额度、不超过吃完卖单要的钱）。
收款钱包需要先授权撮合合约 DAI。
需要的环境变量：OTC_KEEPER_KEY
    python otc_matcher.py            # 检查并撮合
    python otc_matcher.py --dry-run  # 只检查，不发交易
"""
import argparse
import os
import time

from eth_account import Account

from chains import rpc, send_tx
from otc_buyback import DAI, DAI_RESERVE, GAS_TIP, KEEPER_LOW, LGNS_OTC, AIL_OTC, LGNS_PAIR, AIL_PAIR, SOURCE, num, orders, pad, sel, spot, take

MATCHER = "0xdcc5354175d60ffc8364e71e792fcd66366f00fe"  # 撮合合约地址（2026-11-03 部署，区块 18174407）
MAX_ROUNDS = 30
MAX_TX = 5  # 每次最多发 5 笔撮合
MIN_PROFIT = 1 * 10**18  # 至少赚 1 DAI 才撮合（站长 2026-11-08：有空间就秒，1u 都要）
FEE = 0.99  # 每次吃单扣 1%
BAND = None  # 站长 2026-11-07 晚取消：LGNS 在 DEX 卖出扣 50% 税，按 DEX 价划线会误伤正常单；客户挂错价就让机器人秒掉，吃亏学乖。（原来 0.10 = 🛡️ 站长 2026-11-07 定：只撮合离 DEX 市价 10% 以内的单；买单出价高过市价 10%、卖单要价低于市价 10% 的是客户手滑，不吃（前端也不让这种单提交）
TX = "https://anubisscan.io/tx/"


def best_pair(book, now, dex=None):
    live = [o for o in book if o[3] and o[4] > now and o[7] > 0]
    if dex and BAND:  # BAND 为 None 时不过滤（2026-11-07 晚取消价格带）
        live = [o for o in live if ((o[6] / o[5] >= dex * (1 - BAND)) if o[2] else (o[5] / o[6] <= dex * (1 + BAND)))]
    sells = sorted((o for o in live if o[2]), key=lambda o: o[6] / o[5])  # 要价从低到高
    buys = sorted((o for o in live if not o[2]), key=lambda o: -o[5] / o[6])  # 出价从高到低
    for s in sells:
        for b in buys:
            if s[1] == b[1]:
                continue  # 同一个人的买卖单不撮合
            ask, bid = s[6] / s[5], b[5] / b[6]
            if bid * FEE * FEE > ask * 1.002:  # 扣掉两边手续费还有赚
                return s, b
        break
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    keeper = Account.from_key(os.environ["OTC_KEEPER_KEY"]) if os.environ.get("OTC_KEEPER_KEY") else None
    if not MATCHER:
        print("撮合合约还没部署，只检查")
    sent = 0
    for name, otc, pair in (("LGNS", LGNS_OTC, LGNS_PAIR), ("AiLGNS", AIL_OTC, AIL_PAIR)):
        try:
            t, d = spot(pair)
            dex = d / t if t else None  # DAI 原始单位 / 币原始单位，和挂单的 want/give 同一口径
        except Exception:
            dex = None
        while sent < MAX_TX:
            p = best_pair(orders(otc), time.time() + 120, dex)
            if not p:
                print(f"{name}：没有能撮合的单")
                break
            s, b = p
            need = take(s, 10**40)[1]  # 吃完卖单要付的 DAI
            bal = num(DAI, "0x70a08231" + pad(SOURCE)) - DAI_RESERVE
            allow = num(DAI, "0xdd62ed3e" + pad(SOURCE) + pad(MATCHER)) if MATCHER else bal
            dai_in = max(0, min(bal, allow, need))  # 每次动用能用的最多资金
            print(f"{name}：卖单 #{s[0]} 要价 {s[6] / s[5] * (1e-9 if name == 'LGNS' else 1):.6f}，买单 #{b[0]} 出价 {b[5] / b[6] * (1e-9 if name == 'LGNS' else 1):.6f}，"
                  f"周转 {dai_in / 1e18:,.2f} DAI（收款钱包可用 {bal / 1e18:,.2f}，授权 {allow / 1e18:,.2f}，吃完卖单要 {need / 1e18:,.2f}）")
            if dai_in < 10**18:
                print("收款钱包可用 DAI 不到 1，跳过（请检查余额和授权）")
                break
            if args.dry_run or not keeper or not MATCHER:
                break
            tip = GAS_TIP if num(DAI, "0x70a08231" + pad(keeper.address)) < KEEPER_LOW else 0
            data = (sel("matchOrders(address,uint256,uint256,uint256,uint256,uint256,uint256)") + pad(otc) + f"{s[0]:064x}" + f"{b[0]:064x}"
                    + f"{dai_in:064x}" + f"{MAX_ROUNDS:064x}" + f"{MIN_PROFIT:064x}" + f"{tip:064x}")
            try:
                rpc("anubis", "eth_call", [{"from": keeper.address, "to": MATCHER, "data": data}, "latest"])  # 先模拟，赚不到钱就不发
            except Exception as e:
                print(f"模拟不通过，跳过：{e}")
                break
            h = send_tx("anubis", keeper, MATCHER, data, "OTC 撮合")
            print(f"撮合成功：{TX}{h}")
            sent += 1


if __name__ == "__main__":
    main()
