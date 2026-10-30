"""部署 AiLGNS 手续费分账合约（一次性）。用 OTC_KEEPER_KEY 这个钱包付 Gas 部署，并把它设为分账触发钱包。
    python deploy_fee_splitter.py
"""
import json
import os
import time

from eth_account import Account

from ailgns_lp_monitor import rpc

CHAIN_ID = 6714


def main():
    acct = Account.from_key(os.environ["OTC_KEEPER_KEY"])
    art = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "AilgnsFeeSplitter.json")))
    data = art["bytecode"] + acct.address[2:].lower().rjust(64, "0")  # constructor(keeper)
    gas = int(int(rpc("eth_estimateGas", [{"from": acct.address, "data": data}]), 16) * 1.3)
    signed = acct.sign_transaction({
        "data": data, "value": 0, "chainId": CHAIN_ID, "gas": gas,
        "gasPrice": int(rpc("eth_gasPrice", []), 16),
        "nonce": int(rpc("eth_getTransactionCount", [acct.address, "pending"]), 16),
    })
    raw = signed.raw_transaction.hex()
    h = rpc("eth_sendRawTransaction", [raw if raw.startswith("0x") else "0x" + raw])
    print("部署交易", h)
    for _ in range(90):
        r = rpc("eth_getTransactionReceipt", [h])
        if r:
            if r["status"] != "0x1":
                raise RuntimeError("部署失败")
            print("分账合约地址", r["contractAddress"], "区块", int(r["blockNumber"], 16), "触发钱包", acct.address)
            return
        time.sleep(2)
    raise RuntimeError("部署 3 分钟未确认")


if __name__ == "__main__":
    main()
