"""OTC 手续费到账立刻分账：每 5 分钟检查一次，托管合约打进原收款钱包的手续费，按 30% / 40% / 30% 分出去。

分账合约 AilgnsFeeSplitter（地址、比例写死）：30% → 0xe878…91c3，40% → 0xD53A…7285，30% 留在原收款钱包 0xedb9…b1e6。
只分「托管合约 → 原收款钱包」的手续费转账，原收款钱包里其他的钱不动。
已经分了多少记在分账合约链上（totalSplit），所以重复运行也不会多分。
原收款钱包需要先在 ailgns.dev/#/otc/split 授权分账合约，没授权时只提示、不报错。
需要的环境变量：OTC_KEEPER_KEY（就是退款用的那个付 Gas 小钱包，它也是分账合约认可的触发钱包）
    python otc_fee_split.py            # 检查并分账
    python otc_fee_split.py --dry-run  # 只检查
"""
import argparse
import json
import os
import time

from eth_account import Account
from eth_utils import to_checksum_address

from ailgns_lp_monitor import rpc

SPLITTER = "0xeee877c1b6285b82890bdcd027d2715f658ecf23"
START = 17842245  # 分账合约部署的区块，之前的手续费不分
OTC = "0x4e730be6d46be71223ded5a32dc45799f6d4bc15"
SOURCE = "0xedb9c6900a11d2dd37da6b59f3604c35a39db1e6"
TOKENS = {"AiLGNS": "0x256edb652db43e721ec6a12b6b51b6967d3bb8cb", "DAI": "0x83fd06f0846d9d90b3016bf670efe2e0b11cde14"}
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
CHAIN_ID = 6714
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fee_split.json")


def selector(sig):
    from eth_utils import keccak
    return "0x" + keccak(text=sig).hex()[:8]


def pad(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def call(to, data):
    return int(rpc("eth_call", [{"to": to, "data": data}, "latest"]), 16)


def scan(state, latest):
    """把新到的手续费累计进 state["fees"]"""
    start = state.get("scanned_to", START - 1) + 1
    for b in range(start, latest + 1, 5000):
        end = min(latest, b + 4999)
        for name, tok in TOKENS.items():
            logs = rpc("eth_getLogs", [{"address": tok, "fromBlock": hex(b), "toBlock": hex(end), "topics": [TRANSFER, "0x" + pad(OTC), "0x" + pad(SOURCE)]}])
            for l in logs:
                state["fees"][name] = str(int(state["fees"].get(name, "0")) + int(l["data"], 16))
        state["scanned_to"] = end


def send_split(key, token, amount):
    acct = Account.from_key(key)
    data = selector("split(address,uint256)") + pad(token) + f"{amount:064x}"
    gas = int(int(rpc("eth_estimateGas", [{"from": acct.address, "to": SPLITTER, "data": data}]), 16) * 1.3)
    signed = acct.sign_transaction({
        "to": to_checksum_address(SPLITTER), "data": data, "value": 0, "chainId": CHAIN_ID, "gas": gas,
        "gasPrice": int(rpc("eth_gasPrice", []), 16),
        "nonce": int(rpc("eth_getTransactionCount", [acct.address, "pending"]), 16),
    })
    raw = signed.raw_transaction.hex()
    h = rpc("eth_sendRawTransaction", [raw if raw.startswith("0x") else "0x" + raw])
    for _ in range(60):
        r = rpc("eth_getTransactionReceipt", [h])
        if r:
            if r["status"] != "0x1":
                raise RuntimeError(f"分账交易失败: {h}")
            return h
        time.sleep(2)
    raise RuntimeError(f"分账交易 2 分钟未确认: {h}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    state.setdefault("fees", {})
    latest = int(rpc("eth_blockNumber", []), 16)
    scan(state, latest)
    key = os.environ.get("OTC_KEEPER_KEY")
    total_sel = selector("totalSplit(address)")
    for name, tok in TOKENS.items():
        fees = int(state["fees"].get(name, "0"))
        done = call(SPLITTER, total_sel + pad(tok))
        pending = fees - done
        print(f"{name}：累计手续费 {fees / 1e18:.6f}，已分 {done / 1e18:.6f}，待分 {pending / 1e18:.6f}")
        if pending <= 0 or args.dry_run:
            continue
        out = pending - pending * 3000 // 10000  # 要转出去的 70%（留下的 30% 不用转）
        allowance = call(tok, "0xdd62ed3e" + pad(SOURCE) + pad(SPLITTER))
        balance = call(tok, "0x70a08231" + pad(SOURCE))
        if allowance < out:
            print(f"  原收款钱包对分账合约的 {name} 授权不够（还剩 {allowance / 1e18:.6f}，需要 {out / 1e18:.6f}），请到 ailgns.dev/#/otc/split 授权")
            continue
        if balance < out:
            print(f"  原收款钱包里的 {name} 只有 {balance / 1e18:.6f}，不够分，跳过")
            continue
        if not key:
            print("  还没设置 OTC_KEEPER_KEY，跳过")
            continue
        print(f"  已分账：{send_split(key, tok, pending)}")
    if not args.dry_run:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as f:
            json.dump(state, f)


if __name__ == "__main__":
    main()
