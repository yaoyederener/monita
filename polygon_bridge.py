"""Polygon 收入跨链回购：收款钱包在 Polygon 上收到的 LGNS、DAI 攒够 MIN_USD 美金后，
1. 触发钱包按授权把这部分收入取过来（收款钱包要先在 ailgns.dev/#/otc/split 给触发钱包授权 Polygon 的 LGNS、DAI）
2. LGNS 在 QuickSwap（LGNS/DAI 池）卖成 DAI（卖出扣约 5% 税）
3. DAI 通过 Anubis 官方跨链桥（app.anubisbridge.com）跨到 Anubis，收款人写死为收款钱包（桥收固定 1 DAI）
4. 到账后，otc_fee_split.py 把它算作 Anubis 新收入，otc_buyback.py 照常在 OTC 扫单买成 AiLGNS 按 40/30/30 分出去
中途哪一步失败，币留在触发钱包里，下次运行从触发钱包里的余额接着卖 / 跨（不会重复取）。
需要的环境变量：OTC_KEEPER_KEY；SMTP_USER / SMTP_PASS / OPS_EMAILS（可选，跨链成功发邮件）
    python polygon_bridge.py            # 按规则判断要不要跨
    python polygon_bridge.py --dry-run  # 只打印，不发交易
"""
import argparse
import json
import os
import time

from eth_account import Account
from eth_utils import keccak

from chains import rpc, send_tx
from notify_mail import send as send_mail

SOURCE = "0xedb9c6900a11d2dd37da6b59f3604c35a39db1e6"  # 收款钱包（跨过去的 DAI 也只能到这里）
LGNS = "0xeb51d9a39ad5eef215dc0bf39a8821ff804a0f01"  # Polygon LGNS（9 位）
DAI = "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063"  # Polygon DAI
PAIR = "0x882df4b0fb50a229c3b4124eb18c759911485bfb"  # QuickSwap LGNS/DAI，token0 = DAI
ROUTER = "0xa5e0829caced8ffdd4de3c43696c57f7d7a678ff"  # QuickSwap V2 路由（工厂 0x5757…3ab32，和上面池子同一个）
SPLITTER = "0xcc274e07847a6a86917b6ccd4e67706170d4036f"  # Polygon 旧分账合约：它分过的不再算
BRIDGE = "0x8932fe7726c1ee743f662f485c3e5a5d1d595f71"  # Anubis 官方跨链桥（各链同一地址）
DAI_TOKEN_ID = "a5e92f3efb6826155f1f728e162af9d7cda33a574a1153b58f03ea01cc37e568"  # 桥里 DAI 的编号（Polygon、Anubis 相同）
ANUBIS = 6714
MIN_USD = 100  # 站长要求：攒够 100 美金才跨一次（桥每次固定收 1 DAI，最少跨 10 DAI）
BRIDGE_MIN = 10 * 10**18
SELL_TAX = 0.05  # Polygon LGNS 卖出税
FEE_SPLIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fee_split.json")
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "polygon_bridge.json")
TX = "https://polygonscan.com/tx/"


def sel(sig):
    return keccak(text=sig)[:4].hex()


def pad(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def num(to, data):
    return int(rpc("polygon", "eth_call", [{"to": to, "data": "0x" + data.replace("0x", "")}, "latest"]), 16)


def bal(tok, who):
    return num(tok, "70a08231" + pad(who))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    state = json.load(open(STATE)) if os.path.exists(STATE) else {"taken": {"LGNS": "0", "DAI": "0"}, "bridged": []}
    fs = json.load(open(FEE_SPLIT))["tokens"]
    keeper = Account.from_key(os.environ["OTC_KEEPER_KEY"]) if os.environ.get("OTC_KEEPER_KEY") else None
    me = keeper.address if keeper else SOURCE

    # 收款钱包里还没处理的 Polygon 收入 = 累计收入 − 旧分账合约分过的 − 这里已经取过的（再不超过余额和授权）
    take = {}
    for name, tok in (("LGNS", LGNS), ("DAI", DAI)):
        income = int(fs[f"polygon:{tok}"]["income"])
        done = num(SPLITTER, sel("totalSplit(address)") + pad(tok))
        pending = max(income - done - int(state["taken"][name]), 0)
        allow = num(tok, "dd62ed3e" + pad(SOURCE) + pad(me))
        take[name] = min(pending, bal(tok, SOURCE), allow)
        print(f"Polygon {name}：待处理 {pending / 10 ** (9 if name == 'LGNS' else 18):.6f}，授权给触发钱包 {allow / 10 ** (9 if name == 'LGNS' else 18):.4g}"
              + ("　← 授权不够，请到 ailgns.dev/#/otc/split 授权" if allow < pending else ""))

    w = rpc("polygon", "eth_call", [{"to": PAIR, "data": "0x0902f1ac"}, "latest"])[2:]
    r_dai, r_lgns = int(w[:64], 16), int(w[64:128], 16)
    held_lgns, held_dai = (bal(LGNS, me), bal(DAI, me)) if keeper else (0, 0)  # 上次没卖完 / 没跨完、留在触发钱包里的
    lgns_all = take["LGNS"] + held_lgns
    lgns_in = lgns_all * (1 - SELL_TAX) * 0.997
    dai_from_lgns = int(lgns_in * r_dai / (r_lgns + lgns_in)) if lgns_all else 0
    usd = (dai_from_lgns + take["DAI"] + held_dai) / 1e18
    print(f"这批约 {usd:,.2f} 美金（LGNS {lgns_all / 1e9:,.4f} 枚卖出约 {dai_from_lgns / 1e18:,.2f} DAI，DAI {(take['DAI'] + held_dai) / 1e18:,.2f}）")
    if usd < MIN_USD and not (held_lgns or held_dai >= BRIDGE_MIN):
        print(f"不到 {MIN_USD} 美金，攒着下次再跨")
        return
    if args.dry_run or not keeper:
        print("dry-run / 没设置 OTC_KEEPER_KEY：不发交易")
        return

    txs = []
    # 1. 取收入：每取一笔马上记下，后面失败了也不会重复取
    for name, tok in (("LGNS", LGNS), ("DAI", DAI)):
        if take[name] > 0:
            txs.append(send_tx("polygon", keeper, tok, "0x" + sel("transferFrom(address,address,uint256)") + pad(SOURCE) + pad(me) + f"{take[name]:064x}", f"取 Polygon {name}"))
            state["taken"][name] = str(int(state["taken"][name]) + take[name])
            _save(state)
    # 2. LGNS 卖成 DAI（带卖出税的币要用 SupportingFeeOnTransferTokens；最少拿到预估的 90%）
    lgns = bal(LGNS, me)
    if lgns > 0:
        if num(LGNS, "dd62ed3e" + pad(me) + pad(ROUTER)) < lgns:
            send_tx("polygon", keeper, LGNS, "0x095ea7b3" + pad(ROUTER) + "f" * 64, "授权 QuickSwap 卖 LGNS")
        lgns_in = lgns * (1 - SELL_TAX) * 0.997
        min_out = int(lgns_in * r_dai / (r_lgns + lgns_in) * 0.9)
        data = ("0x" + sel("swapExactTokensForTokensSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)")
                + f"{lgns:064x}" + f"{min_out:064x}" + f"{0xa0:064x}" + pad(me) + f"{int(time.time()) + 600:064x}"
                + f"{2:064x}" + pad(LGNS) + pad(DAI))
        txs.append(send_tx("polygon", keeper, ROUTER, data, "LGNS 卖成 DAI"))
    # 3. DAI 跨到 Anubis，收款人写死为收款钱包
    dai = bal(DAI, me)
    if dai < BRIDGE_MIN:
        print(f"触发钱包里只有 {dai / 1e18:.2f} DAI，不到跨链桥最低 10 DAI，下次再跨")
        return
    if num(DAI, "dd62ed3e" + pad(me) + pad(BRIDGE)) < dai:
        send_tx("polygon", keeper, DAI, "0x095ea7b3" + pad(BRIDGE) + f"{dai:064x}", "授权跨链桥 DAI")
    h = send_tx("polygon", keeper, BRIDGE, "0x" + sel("bridge(bytes32,uint32,bytes32,uint256)") + DAI_TOKEN_ID + f"{ANUBIS:064x}" + pad(SOURCE) + f"{dai:064x}", "跨链到 Anubis")
    txs.append(h)
    state["bridged"] = (state["bridged"] + [{"at": int(time.time()), "dai": dai / 1e18, "tx": h}])[-50:]
    _save(state)
    msg = (f"Polygon 收入跨链回购：{dai / 1e18:,.2f} DAI 已从 Polygon 跨到 Anubis 收款钱包（跨链桥收约 1 DAI），"
           f"到账后自动在 OTC 扫单买成 AiLGNS 按 40/30/30 分出去。\n" + "\n".join(TX + t for t in txs))
    print(msg)
    ops = [e.strip() for e in (os.environ.get("OPS_EMAILS") or "").split(",") if e.strip()]
    send_mail("AiLGNS Polygon 收入跨链回购", msg + "\n\n—— AiLGNS 服务站", ops or None)


def _save(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(state, f)


if __name__ == "__main__":
    main()
