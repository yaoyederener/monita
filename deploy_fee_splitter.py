"""部署 AiLGNS 分账合约（每条链一次）。用 OTC_KEEPER_KEY 这个钱包付 Gas 部署，并把它设为分账触发钱包。
    python deploy_fee_splitter.py anubis
    python deploy_fee_splitter.py polygon
"""
import json
import os
import sys

from eth_account import Account

from chains import send_tx


def main():
    chain = sys.argv[1] if len(sys.argv) > 1 else "anubis"
    acct = Account.from_key(os.environ["OTC_KEEPER_KEY"])
    art = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "AilgnsFeeSplitter.json")))
    data = art["bytecode"] + acct.address[2:].lower().rjust(64, "0")  # constructor(keeper)
    h, addr, block = send_tx(chain, acct, None, data, "部署")
    print(f"链 {chain} 部署交易 {h}")
    print(f"分账合约地址 {addr} 区块 {block} 触发钱包 {acct.address}")


if __name__ == "__main__":
    main()
