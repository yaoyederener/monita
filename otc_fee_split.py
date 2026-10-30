"""原收款钱包收入到账立刻分账：每 5 分钟检查一次，转进 0xedb9…b1e6 的收入按 30% / 40% / 30% 分出去。

分账合约 AilgnsFeeSplitter（地址、比例写死，Anubis 和 Polygon 各一个）：
  30% → 0xe878…91c3，40% → 0xD53A…7285，30% 留在原收款钱包 0xedb9…b1e6。
分哪些币：Anubis 链 LGNS / AiLGNS / DAI，Polygon 链 DAI / LGNS（会员费 + OTC 手续费）。
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
]
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
OTC = "0x4e730be6d46be71223ded5a32dc45799f6d4bc15"  # 从托管合约转来的是 OTC 成交手续费
EXPLORERS = {"anubis": "https://anubisscan.io/tx/", "polygon": "https://polygonscan.com/tx/"}
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fee_split.json")


def sel(sig):
    return "0x" + keccak(text=sig).hex()[:8]


def pad(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def call(chain, to, data):
    return int(rpc(chain, "eth_call", [{"to": to, "data": data}, "latest"]), 16)


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
                kind = "OTC 成交手续费" if frm == OTC else "会员付款 / 转入"
                received.append(f"· {kind}：{int(l['data'], 16) / 10 ** dec:,.4f} {label}\n  来自 {frm}\n  {EXPLORERS[chain]}{l['transactionHash']}")
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
            split_done.append(f"· {amt:,.4f} {label}：30% {amt * 0.3:,.4f} → 0xe878…91c3，40% {amt * 0.4:,.4f} → 0xD53A…7285，30% 留在 0xedb9…b1e6\n  {EXPLORERS[chain]}{h}")
        except Exception as e:  # 一个币失败不影响其他币，下次再试
            print(f"  分账失败，下次再试：{e}")

    if (received or split_done) and not args.dry_run:
        parts = []
        if received:
            parts.append(f"收到 {len(received)} 笔付款：\n" + "\n".join(received))
        if split_done:
            parts.append(f"已分账 {len(split_done)} 笔：\n" + "\n".join(split_done))
        title = "AiLGNS 服务站：" + "、".join((["收到付款"] if received else []) + (["已分账"] if split_done else []))
        send_mail(title, "\n\n".join(parts) + "\n\n—— ailgns.dev 自动通知")
    if not args.dry_run:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as f:
            json.dump(state, f)


if __name__ == "__main__":
    main()
