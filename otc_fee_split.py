"""原收款钱包收入到账立刻分账：每 5 分钟检查一次，转进 0xedb9…b1e6 的收入按 30% / 40% / 30% 分出去。
分账照常每 5 分钟做；邮件通知不再一笔一发，先记在状态文件里，每天北京时间 9 点后汇总发一封（收款汇总给站长，运营费用汇总给 OPS_EMAILS）。

分账合约 AilgnsFeeSplitter（地址、比例写死，Anubis 和 Polygon 各一个）：
  30% → 0xe878…91c3，40% → 0xD53A…7285，30% 留在原收款钱包 0xedb9…b1e6。
分哪些币：Anubis 链 LGNS / AiLGNS / DAI / gLGNS，Polygon 链 DAI / LGNS（会员费 + OTC 手续费）。
从下面 START 区块开始，转进原收款钱包的这几种币全部算收入（包括任何人转进来的）。
已经分了多少记在分账合约链上（totalSplit），所以重复运行也不会多分。
原收款钱包需要先在 ailgns.dev/#/otc/split 授权各自链上的分账合约，没授权时只提示、不报错。
需要的环境变量：OTC_KEEPER_KEY（付 Gas 的小钱包，也是分账合约认可的触发钱包；Polygon 上用 POL 付 Gas）
    python otc_fee_split.py            # 检查并分账
    python otc_fee_split.py --dry-run  # 只检查
"""
import argparse
import json
import os
import time

from eth_account import Account
from eth_utils import keccak

from chains import rpc, send_tx
from notify_mail import send as send_mail

SOURCE = "0xedb9c6900a11d2dd37da6b59f3604c35a39db1e6"
SPLITTERS = {"anubis": "0xeee877c1b6285b82890bdcd027d2715f658ecf23", "polygon": "0xcc274e07847a6a86917b6ccd4e67706170d4036f"}
# [链, 名称, 代币, 从哪个区块开始算收入, 每次查日志的最大区块跨度, 小数位]
TOKENS = [
    ["anubis", "LGNS", "0x4d1d808a081fdac440703b3765fc61f8028c06b8", 17842792, 5000, 9],
    ["anubis", "AiLGNS", "0x256edb652db43e721ec6a12b6b51b6967d3bb8cb", 17842792, 5000, 18],
    ["anubis", "DAI", "0x83fd06f0846d9d90b3016bf670efe2e0b11cde14", 17842792, 5000, 18],
    ["polygon", "DAI", "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063", 96447842, 9000, 18],
    ["polygon", "LGNS", "0xeb51d9a39ad5eef215dc0bf39a8821ff804a0f01", 96455222, 9000, 9],
    ["anubis", "gLGNS", "0xf140a92c156e76f5a1d3d6f96284b250330f8f4c", 18004618, 5000, 18],  # gLGNS OTC 手续费（从托管合约部署区块起）
]
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# 从托管合约转来的是 OTC 成交手续费，从像素墙收款合约转来的是像素墙收入
# 按链分开：gLGNS 托管合约（Anubis）和像素墙收款合约（Polygon）地址相同（同一部署钱包、相同 nonce），不能只看地址
OTC = {"anubis": {"0x4e730be6d46be71223ded5a32dc45799f6d4bc15", "0xb6a7a3be9a17ad241665ab9bddbd9a573d488818", "0x2b0ab121d60f823fffc4cef75af26055df388e2b", "0x8c67912b3ed44f4512c2f22b7b2263f6659396ed", "0x8d84d678d195e770caad263d1c37967ec796165c", "0x242ab392571d325d814a48892ac16115a6d285dc"},  # AiLGNS、LGNS、gLGNS、AWAKE NFT 卡牌、AiLGNS 借款（借款手续费是出借人直接付到收款钱包的，这里只是归类）
       "polygon": {"0x4060f38d3e837843b69014d9f85ecfb3e29fa2db"}}  # LGNS
PIXEL_SINKS = {"anubis": {"0xf1fb8f098979dc29a3020070ac93ef24f1b2ca4c"}, "polygon": {"0x2b0ab121d60f823fffc4cef75af26055df388e2b"}}  # 像素墙收款合约
EXPLORERS = {"anubis": "https://anubisscan.io/tx/", "polygon": "https://polygonscan.com/tx/"}
MAIL_HOUR = 9  # 北京时间几点后发前一天的汇总邮件
MAIL_KEEP = 500  # 邮件一直发不出去时，最多攒这么多条
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fee_split.json")


def sel(sig):
    return "0x" + keccak(text=sig).hex()[:8]


def pad(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def call(chain, to, data):
    return int(rpc(chain, "eth_call", [{"to": to, "data": data}, "latest"]), 16)


def bj_now():
    return time.gmtime(time.time() + 8 * 3600)


def digest(state, dry_run):
    """每天发一次汇总邮件：发成功才清空，发失败下次再试"""
    q = state.setdefault("mail", {"received": [], "split": [], "sent": ""})
    now = bj_now()
    today = time.strftime("%Y-%m-%d", now)
    if dry_run or now.tm_hour < MAIL_HOUR or q["sent"] == today:
        return
    stamp = f"截至北京时间 {now.tm_mon}月{now.tm_mday}日 {MAIL_HOUR}:00"

    def totals(items):
        t = {}
        for x in items:
            t[x["label"]] = t.get(x["label"], 0) + x["amt"]
        return "\n".join(f"  {v:,.4f} {k}" for k, v in t.items())

    ok = True
    if q["received"]:
        r = q["received"]
        lines = [f"· {x['kind']}：{x['amt']:,.4f} {x['label']}\n  来自 {x['from']}\n  {x['url']}" for x in r]
        body = f"过去一天共收到 {len(r)} 笔付款（{stamp}）。\n\n合计：\n{totals(r)}\n\n明细：\n" + "\n".join(lines) + "\n\n—— ailgns.dev 自动通知（每天汇总一次）"
        if send_mail(f"AiLGNS 服务站：每日收款汇总（{len(r)} 笔）", body):
            q["received"] = []
        else:
            ok = False
    ops = [e.strip() for e in (os.environ.get("OPS_EMAILS") or "").split(",") if e.strip()]
    if q["split"] and ops:
        sp = q["split"]
        lines = [f"· {x['amt']:,.4f} {x['label']}\n  {x['url']}" for x in sp]
        body = f"过去一天运营费用共发放 {len(sp)} 笔（{stamp}），用于 LGNS 销毁等运营支出。\n\n合计：\n{totals(sp)}\n\n明细：\n" + "\n".join(lines) + "\n\n—— AiLGNS 服务站（每天汇总一次）"
        if send_mail(f"AiLGNS 运营费用发放日报（{len(sp)} 笔）", body, ops):
            q["split"] = []
        else:
            ok = False
    if ok:
        q["sent"] = today


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    if state.get("v") != 2:  # 旧版只记手续费，换成按全部收入重新记
        state = {"v": 2, "tokens": {}}
    key = os.environ.get("OTC_KEEPER_KEY")
    acct = Account.from_key(key) if key else None
    latest = {c: int(rpc(c, "eth_blockNumber", []), 16) for c in SPLITTERS}
    received, split_done = [], []

    for chain, name, tok, start, span, dec in TOKENS:
        label = f"{name}（{'Anubis' if chain == 'anubis' else 'Polygon'}）"
        st = state["tokens"].setdefault(f"{chain}:{tok}", {"scanned_to": start - 1, "income": "0"})
        income = int(st["income"])
        for b in range(st["scanned_to"] + 1, latest[chain] + 1, span):
            end = min(latest[chain], b + span - 1)
            logs = rpc(chain, "eth_getLogs", [{"address": tok, "fromBlock": hex(b), "toBlock": hex(end), "topics": [TRANSFER, None, "0x" + pad(SOURCE)]}])
            income += sum(int(l["data"], 16) for l in logs)
            for l in logs:  # 记下每一笔，最后发邮件
                frm = "0x" + l["topics"][1][26:]
                kind = "OTC 成交手续费" if frm in OTC[chain] else "像素墙收入（20%）" if frm in PIXEL_SINKS[chain] else "会员付款 / 转入"
                received.append({"kind": kind, "amt": int(l["data"], 16) / 10 ** dec, "label": label, "from": frm, "url": EXPLORERS[chain] + l["transactionHash"]})
            st["scanned_to"] = end
        st["income"] = str(income)
        splitter = SPLITTERS[chain]
        done = call(chain, splitter, sel("totalSplit(address)") + pad(tok))
        pending = income - done
        print(f"{label}：累计收入 {income / 10 ** dec:.6f}，已分 {done / 10 ** dec:.6f}，待分 {pending / 10 ** dec:.6f}")
        if pending <= 0 or args.dry_run:
            continue
        out = pending - pending * 3000 // 10000  # 要转出去的 70%（留下的 30% 不用转）
        allowance = call(chain, tok, "0xdd62ed3e" + pad(SOURCE) + pad(splitter))
        balance = call(chain, tok, "0x70a08231" + pad(SOURCE))
        if allowance < out:
            print(f"  原收款钱包对分账合约的授权不够（还剩 {allowance / 10 ** dec:.6f}，需要 {out / 10 ** dec:.6f}），请到 ailgns.dev/#/otc/split 授权")
            continue
        if balance < out:
            print(f"  原收款钱包里只有 {balance / 10 ** dec:.6f}，不够分，跳过")
            continue
        if not acct:
            print("  还没设置 OTC_KEEPER_KEY，跳过")
            continue
        try:
            h = send_tx(chain, acct, splitter, sel('split(address,uint256)') + pad(tok) + f'{pending:064x}', '分账')
            print(f"  已分账：{h}")
            amt = pending / 10 ** dec
            split_done.append({"amt": amt * 0.7, "label": label, "url": EXPLORERS[chain] + h})  # 转出去的 70% 记为本次发放的运营费用
        except Exception as e:  # 一个币失败不影响其他币，下次再试
            print(f"  分账失败，下次再试：{e}")

    # 先记下来，每天汇总发一封（不再一笔一发）
    q = state.setdefault("mail", {"received": [], "split": [], "sent": time.strftime("%Y-%m-%d", bj_now())})  # 刚改成汇总的这天不再补发，明天 9 点发第一封
    q["received"] = (q["received"] + received)[-MAIL_KEEP:]
    q["split"] = (q["split"] + split_done)[-MAIL_KEEP:]
    digest(state, args.dry_run)
    if not args.dry_run:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as f:
            json.dump(state, f)


if __name__ == "__main__":
    main()
