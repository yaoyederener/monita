"""部署 AiLGNS 收入回购分账合约（Anubis 链，一次）。用 OTC_KEEPER_KEY 这个钱包付 Gas 部署，并把它设为触发钱包。
    python deploy_buyback.py
部署后把合约地址和区块填进 otc_buyback.py 的 BUYBACK、START。
"""
import json
import os

from eth_account import Account

from chains import send_tx


def main():
    acct = Account.from_key(os.environ["OTC_KEEPER_KEY"])
    art = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "AilgnsBuyback.json")))
    data = art["bytecode"] + acct.address[2:].lower().rjust(64, "0")  # constructor(keeper)
    h, addr, block = send_tx("anubis", acct, None, data, "部署")
    print(f"部署交易 {h}")
    print(f"回购分账合约地址 {addr} 区块 {block} 触发钱包 {acct.address}")


if __name__ == "__main__":
    main()
