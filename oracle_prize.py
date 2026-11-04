"""🔮 AiLGNS 每日预言家：从域名收入里留出 20% 当奖金，并把奖金自动发给中奖的人。

钱怎么走（不改任何合约）：
  域名注册 / 续费的钱直接进回购分账合约 BUYBACK（0x99f4）。
  ① 每次运行先读两个域名合约的 Paid 事件，累计「应留奖金」= 域名收入（costDai）× 20%（站长自己付的不算，和网站日报同一口径）。
  ② BUYBACK 里有 DAI 时，用合约自带的 refund(DAI) 把它退回收款钱包 SOURCE：
     其中「应留奖金」那部分留在 SOURCE 当奖金池；其余部分记进 otc_buyback.py 的待处理收入，同一次运行里马上被回购分账取走。
     （先从 DAI 里留，不够再从 AiLGNS 里按现价留；用 LGNS 付的域名费，应留奖金先记账，等下次有 DAI / AiLGNS 进来再留。）
  发奖先用 DAI，DAI 不够用 AiLGNS（按现价折算）。
  ③ 读 ailgns.dev/api/oracle?pay=1 的待发奖金，用触发钱包按 SOURCE 给它的 DAI 授权额度 transferFrom(SOURCE → 中奖钱包)，发完回报网站。
必须在 otc_buyback.py 之前运行（工作流里排在它前面）。
需要：OTC_KEEPER_KEY（触发钱包）、BACKUP_KEY（读写网站发奖记录）；SOURCE 先给触发钱包授权 DAI 发奖额度（网站预言家页面用 0xedb9 连接后有按钮）。
    python oracle_prize.py --dry-run   # 只算，不发交易
"""
import argparse
import json
import os
import urllib.request

from eth_account import Account
from eth_utils import keccak

from chains import rpc, send_tx
from notify_mail import send as send_mail

BUYBACK = "0x99f47ca8e7ed873e01a337aefc5b75f5328d2da3"
SOURCE = "0xedb9c6900a11d2dd37da6b59f3604c35a39db1e6"
DAI = "0x83fd06f0846d9d90b3016bf670efe2e0b11cde14"
AIL = "0x256edb652db43e721ec6a12b6b51b6967d3bb8cb"
AIL_PAIR = "0x0a1cce65d995c6c5100e6f5ba3c388ef34cecaf0"  # token0 = AiLGNS，token1 = DAI
NAMES = ["0xbc408e9d7350967e7247c917a086cb4ece0ed988", "0x93af23fe74e5180f54f0996fa6bb6c157775f7f3"]
PAID = "0x26c4308021cef5cdbc618fee8b61faf9de6b8f7e4e49fac43869dc1a26756408"  # Paid(payer, token, amount, costDai)
OWN = {SOURCE, "0x89dd06754bb239b6c4c92bc47c8fa72889084e27"}  # 站长自己付的不算收入
START = 18161697  # 域名合约（第 3 版）部署区块
START_TS = 1793808000  # 2026-11-05 00:00 北京时间：从这一刻起的域名收入才留奖金（第一期 11 月 6 日开奖 = 11 月 5 日收入 × 20%）
SHARE_BPS = 2000  # 20%
SITE = "https://ailgns.dev/api/oracle"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "oracle_prize.json")
BUY_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "buyback.json")
TX = "https://anubisscan.io/tx/"


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
    for b in range(st["scanned_to"] + 1, latest + 1, 5000):
        end = min(latest, b + 4999)
        for c in NAMES:
            for l in rpc("anubis", "eth_getLogs", [{"address": c, "fromBlock": hex(b), "toBlock": hex(end), "topics": [PAID]}]):
                if "0x" + l["topics"][1][26:] in OWN:
                    continue
                if not st.get("start_ok"):  # 开始时间前的收入不算
                    bt = int(rpc("anubis", "eth_getBlockByNumber", [l["blockNumber"], False])["timestamp"], 16)
                    if bt < START_TS:
                        continue
                new_cost += int(l["data"][2 + 64:2 + 128], 16)
        st["scanned_to"] = end
        if not st.get("start_ok") and int(rpc("anubis", "eth_getBlockByNumber", [hex(end), False])["timestamp"], 16) >= START_TS:
            st["start_ok"] = True
    st["income"] = str(int(st["income"]) + new_cost)
    owed = int(st["owed"]) + new_cost * SHARE_BPS // 10000
    held, held_ail = int(st["held"]), int(st.get("held_ail", "0"))
    w = rpc("anubis", "eth_call", [{"to": AIL_PAIR, "data": "0x0902f1ac"}, "latest"])[2:]
    r_ail, r_dai = int(w[:64], 16), int(w[64:128], 16)  # AiLGNS 现价 = r_dai / r_ail
    print(f"新域名收入 {new_cost / 1e18:.2f} DAI；应留奖金 {owed / 1e18:.2f}，已留 {held / 1e18:.2f} DAI + {held_ail / 1e18:.6f} AiLGNS")

    def save():
        st["owed"], st["held"], st["held_ail"] = str(owed), str(held), str(held_ail)
        with open(STATE, "w") as f:
            json.dump(st, f)

    keeper = Account.from_key(os.environ["OTC_KEEPER_KEY"]) if os.environ.get("OTC_KEEPER_KEY") else None
    # ② 从 BUYBACK 里的 DAI 留出奖金（退回 SOURCE，多退的记回 otc_buyback 的待处理收入）
    for tok, name in ((DAI, "DAI"), (AIL, "AiLGNS")):
        carry = num(tok, "0x70a08231" + pad(BUYBACK))
        if owed <= 0 or carry <= 0 or not keeper or dry:
            continue
        want = owed if tok == DAI else owed * r_ail // r_dai  # AiLGNS 按现价折算
        keep = min(carry, want)
        h = send_tx("anubis", keeper, BUYBACK, sel("refund(address)") + pad(tok), f"退回 {name} 留奖金")
        bs = json.load(open(BUY_STATE))
        bs[tok]["income"] = str(int(bs[tok]["income"]) + carry - keep)  # 多退的这部分由 otc_buyback.py 照常回购分账
        with open(BUY_STATE, "w") as f:
            json.dump(bs, f)
        if tok == DAI:
            owed, held = owed - keep, held + keep
        else:
            owed, held_ail = max(0, owed - keep * r_dai // r_ail), held_ail + keep
        save()  # 退回已经上链：两份记账马上存，后面出错也不会错账
        print(f"退回 {carry / 1e18:.6f} {name} 到收款钱包：留 {keep / 1e18:.6f} 当奖金，其余照常回购分账 {TX}{h}")

    # ③ 发奖
    key = os.environ.get("BACKUP_KEY")
    sent, short = [], []
    if key:
        todo = http(SITE + "?pay=1&key=" + key).get("pay", [])
        for p in todo:
            amt = int(round(p["amt"] * 100)) * 10**16
            if not keeper or dry:
                short.append(f"{p['name']} {p['amt']} DAI（dry-run）")
                continue
            # 先用 DAI 发；DAI 不够就用 AiLGNS 按现价折算
            tok, n = (DAI, amt) if amt <= held else (AIL, amt * r_ail // r_dai)
            pool = held if tok == DAI else held_ail
            allow = num(tok, "0xdd62ed3e" + pad(SOURCE) + pad(keeper.address))
            bal = num(tok, "0x70a08231" + pad(SOURCE))
            if n > pool or n > allow or n > bal:
                short.append(f"{p['name']} {p['amt']} DAI（奖金池 {held / 1e18:.2f} DAI + {held_ail / 1e18:.4f} AiLGNS，授权 {allow / 1e18:.4f}）")
                continue
            data = "0x23b872dd" + pad(SOURCE) + pad(p["a"]) + f"{n:064x}"  # transferFrom(SOURCE, 中奖钱包, n)
            h = send_tx("anubis", keeper, tok, data, "发预言家奖金")
            if tok == DAI:
                held -= n
            else:
                held_ail -= n
            save()
            http(SITE, {"action": "paid", "id": p["id"], "tx": h, "key": key})
            st["paid"] = (st.get("paid") or [])[-200:] + [{"id": p["id"], "amt": p["amt"], "tx": h}]
            sent.append(f"{p['day']} {'🎯 大奖' if p['kind'] == 'jackpot' else '🥇 最接近'} {p['name']}：{p['amt']} DAI {TX}{h}")
    if not dry:
        save()
    if sent:
        print("\n".join(sent))
        ops = [e.strip() for e in (os.environ.get("OPS_EMAILS") or "").split(",") if e.strip()]
        send_mail("AiLGNS 预言家奖金已发", "\n".join(sent) + "\n\n—— AiLGNS 服务站", ops or None)
    if short:
        print("还没发（奖金池或授权不够）：" + "；".join(short))


if __name__ == "__main__":
    main()
