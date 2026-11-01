"""OTC 托管合约过期自动退回：找出已过期还没退回的挂单，调用 refundExpired 把币退给挂单人。
三个市场各一个托管合约：AiLGNS（Anubis）、LGNS（Anubis）、LGNS（Polygon，Gas 用 POL）。
顺便把像素墙收款合约（Anubis、Polygon 各一个）里的 LGNS 分出去（flush：80% 销毁、20% 转原收款钱包），任何人都能调用。

合约只会把币退给挂单人本人 / 转到写死的地址，这个钱包只负责付 Gas，碰不到任何人的币。
需要的环境变量：OTC_KEEPER_KEY（专用小钱包私钥，放少量 Anubis DAI 和 Polygon POL 付 Gas）
    python otc_refund_keeper.py            # 检查并退回
    python otc_refund_keeper.py --dry-run  # 只检查
"""
import argparse
import os

from eth_account import Account

from chains import rpc, send_tx

# [名称, 链, 托管合约, 每张挂单占几个字]
# 代币托管（AilgnsOtcEscrow）每张 7 个字：maker, sell, open, expiry, giveTotal, wantTotal, giveLeft
# 卡牌托管（AwakeNftEscrow）每张 6 个字：maker, sell, open, expiry, tokenId, price
# 借款合约（AilgnsLoanEscrow）每张 9 个字：borrower, lender, status, expiry, term, due, collateral, principal, repayAmount
#   status 0 挂单中且 expiry 到了 → refundExpired 退抵押币；status 1 借款中且 due 到了 → claim 把抵押币划给出借人（任何人都能触发，币只给出借人）
ESCROWS = [
    ["AiLGNS", "anubis", "0x4e730be6d46be71223ded5a32dc45799f6d4bc15", 7],
    ["LGNS（Anubis）", "anubis", "0xb6a7a3be9a17ad241665ab9bddbd9a573d488818", 7],
    ["LGNS（Polygon）", "polygon", "0x4060f38d3e837843b69014d9f85ecfb3e29fa2db", 7],
    ["gLGNS（Anubis）", "anubis", "0x2b0ab121d60f823fffc4cef75af26055df388e2b", 7],
    ["AWAKE NFT 卡牌（Anubis）", "anubis", "0x8c67912b3ed44f4512c2f22b7b2263f6659396ed", 6],
    ["AiLGNS 抵押借款（Anubis）", "anubis", "", 9],
]
LOAN_SEL_COUNT = "0x9d11ed75"  # loansCount()
LOAN_SEL_GET = "0x86b20104"  # getLoans(uint256,uint256)
LOAN_SEL_CLAIM = "0x379607f5"  # claim(uint256)
# 像素墙收款合约 [链, 合约, LGNS]；合约地址空着表示还没部署（付款直接进销毁地址，不用分）
PIXEL_SINKS = [
    ["anubis", "0xf1fb8f098979dc29a3020070ac93ef24f1b2ca4c", "0x4d1d808a081fdac440703b3765fc61f8028c06b8"],
    ["polygon", "0x2b0ab121d60f823fffc4cef75af26055df388e2b", "0xeb51d9a39ad5eef215dc0bf39a8821ff804a0f01"],
]
GAS_NAME = {"anubis": "DAI", "polygon": "POL"}
SEL_COUNT = "0x35daa731"  # ordersCount()
SEL_GET = "0x8f72fc77"  # getOrders(uint256,uint256)
SEL_REFUND = "0x34bfca5c"  # refundExpired(uint256[])
SEL_FLUSH = "0x6b9f96ea"  # flush()
BATCH = 50  # 一笔交易最多退回多少张


def u256(v):
    return f"{int(v):064x}"


def expired_orders(chain, otc, now, width=7):
    """返回 (总数, 要退回的编号, 要触发违约领取的编号)"""
    loan = width == 9
    count = int(rpc(chain, "eth_call", [{"to": otc, "data": LOAN_SEL_COUNT if loan else SEL_COUNT}, "latest"]), 16)
    ids, claims = [], []
    for start in range(0, count, 500):
        h = rpc(chain, "eth_call", [{"to": otc, "data": (LOAN_SEL_GET if loan else SEL_GET) + u256(start) + u256(500)}, "latest"])[2:]
        w = [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]
        n = w[1]
        for i in range(n):
            o = w[2 + i * width: 2 + (i + 1) * width]
            if loan:
                status, expiry, due = o[2], o[3], o[5]
                if status == 0 and expiry <= now:
                    ids.append(start + i)
                elif status == 1 and due <= now:
                    claims.append(start + i)
                continue
            is_open, expiry = o[2], o[3]
            if is_open and expiry <= now and (width == 6 or o[6] > 0):  # 卡牌挂单只要还开着就退（卡或 DAI 一定还在合约里）
                ids.append(start + i)
    return count, ids, claims


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    key = os.environ.get("OTC_KEEPER_KEY")
    acct = Account.from_key(key) if key else None
    if acct:
        for chain in ("anubis", "polygon"):
            gas = int(rpc(chain, "eth_getBalance", [acct.address, "latest"]), 16) / 1e18
            low = gas < (0.05 if chain == "anubis" else 0.5)
            print(f"退回钱包 {acct.address}，{chain} Gas 余额 {gas:.4f} {GAS_NAME[chain]}" + ("（余额太低，请充值）" if low else ""))

    for name, chain, otc, width in ESCROWS:
        if not otc:
            continue
        try:  # 一个市场出问题不影响别的市场
            now = int(rpc(chain, "eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
            count, ids, claims = expired_orders(chain, otc, now, width)
            print(f"{name}：共 {count} 张挂单，已过期待退回 {len(ids)} 张：{ids}" + (f"，到期未还待划给出借人 {len(claims)} 笔：{claims}" if claims else ""))
            if (not ids and not claims) or args.dry_run:
                continue
            if not acct:
                print("  还没设置 OTC_KEEPER_KEY，本次跳过（挂单人可以在网站上自己取回）")
                continue
            for i in range(0, len(ids), BATCH):
                part = ids[i:i + BATCH]
                data = SEL_REFUND + u256(32) + u256(len(part)) + "".join(u256(x) for x in part)
                print(f"  已退回 {part}：{send_tx(chain, acct, otc, data, '退回')}")
            for cid in claims:  # 借款到期没还：把抵押币划给出借人（出借人自己也能在网站上点）
                try:
                    print(f"  借款 #{cid} 到期未还，抵押币已划给出借人：{send_tx(chain, acct, otc, LOAN_SEL_CLAIM + u256(cid), '违约领取')}")
                except Exception as e:
                    print(f"  借款 #{cid} 违约领取失败，下次再试：{e}")
        except Exception as e:
            print(f"  {name} 退回失败，下次再试：{e}")

    # 像素墙收款合约：有 LGNS 就分出去（80% 销毁、20% 转原收款钱包，之后由 otc_fee_split.py 按 30/40/30 分）
    for chain, sink, lgns in PIXEL_SINKS:
        if not sink:
            continue
        try:
            bal = int(rpc(chain, "eth_call", [{"to": lgns, "data": "0x70a08231" + u256(int(sink, 16))}, "latest"]), 16)
            print(f"像素墙收款合约（{chain}）待分：{bal / 1e9:.4f} LGNS")
            if bal > 0 and not args.dry_run and acct:
                print(f"  已分出（80% 销毁、20% 运营）：{send_tx(chain, acct, sink, SEL_FLUSH, '像素墙分账')}")
        except Exception as e:
            print(f"  像素墙分账（{chain}）失败，下次再试：{e}")


if __name__ == "__main__":
    main()
