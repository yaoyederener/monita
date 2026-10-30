"""OTC 托管合约过期自动退回：找出已过期还没退回的挂单，调用 refundExpired 把币退给挂单人。

合约只会把币退给挂单人本人，这个钱包只负责付 Gas，碰不到任何人的币。
需要的环境变量：OTC_KEEPER_KEY（专用小钱包私钥，放少量 Anubis DAI 付 Gas）
    python otc_refund_keeper.py            # 检查并退回
    python otc_refund_keeper.py --dry-run  # 只检查
"""
import argparse
import os
import time

from eth_account import Account
from eth_utils import to_checksum_address

from ailgns_lp_monitor import rpc

OTC = "0x4e730be6d46be71223ded5a32dc45799f6d4bc15"
CHAIN_ID = 6714
SEL_COUNT = "0x35daa731"  # ordersCount()
SEL_GET = "0x8f72fc77"  # getOrders(uint256,uint256)
SEL_REFUND = "0x34bfca5c"  # refundExpired(uint256[])
BATCH = 50  # 一笔交易最多退回多少张


def u256(v):
    return f"{int(v):064x}"


def expired_orders(now):
    count = int(rpc("eth_call", [{"to": OTC, "data": SEL_COUNT}, "latest"]), 16)
    ids = []
    for start in range(0, count, 500):
        h = rpc("eth_call", [{"to": OTC, "data": SEL_GET + u256(start) + u256(500)}, "latest"])[2:]
        w = [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]
        n = w[1]
        for i in range(n):
            o = w[2 + i * 7: 9 + i * 7]
            is_open, expiry, give_left = o[2], o[3], o[6]
            if is_open and expiry <= now and give_left > 0:
                ids.append(start + i)
    return count, ids


def refund(ids, key):
    acct = Account.from_key(key)
    data = SEL_REFUND + u256(32) + u256(len(ids)) + "".join(u256(i) for i in ids)
    tx = {"from": acct.address, "to": OTC, "data": data}
    gas = int(int(rpc("eth_estimateGas", [tx]), 16) * 1.3)
    signed = acct.sign_transaction({
        "to": to_checksum_address(OTC),  # 签名要求带大小写校验的地址 "data": data, "value": 0, "chainId": CHAIN_ID, "gas": gas,
        "gasPrice": int(rpc("eth_gasPrice", []), 16),
        "nonce": int(rpc("eth_getTransactionCount", [acct.address, "pending"]), 16),
    })
    raw = signed.raw_transaction.hex()
    h = rpc("eth_sendRawTransaction", [raw if raw.startswith("0x") else "0x" + raw])
    for _ in range(60):
        r = rpc("eth_getTransactionReceipt", [h])
        if r:
            if r["status"] != "0x1":
                raise RuntimeError(f"退回交易失败: {h}")
            return h
        time.sleep(2)
    raise RuntimeError(f"退回交易 2 分钟未确认: {h}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    key = os.environ.get("OTC_KEEPER_KEY")
    if key:
        addr = Account.from_key(key).address
        gas = int(rpc("eth_getBalance", [addr, "latest"]), 16) / 1e18
        print(f"退回钱包 {addr}，Gas 余额 {gas:.4f} DAI" + ("（余额太低，请充值）" if gas < 0.05 else ""))
    latest = rpc("eth_getBlockByNumber", ["latest", False])
    count, ids = expired_orders(int(latest["timestamp"], 16))
    print(f"共 {count} 张挂单，已过期待退回 {len(ids)} 张：{ids}")
    if not ids or args.dry_run:
        return
    if not key:
        print("还没设置 OTC_KEEPER_KEY，本次跳过（挂单人可以在网站上自己取回）")
        return
    for i in range(0, len(ids), BATCH):
        part = ids[i:i + BATCH]
        print(f"已退回 {part}：{refund(part, key)}")


if __name__ == "__main__":
    main()
