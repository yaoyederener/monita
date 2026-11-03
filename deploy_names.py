"""部署 .ailgns 域名合约（Anubis 链，一次）。用 OTC_KEEPER_KEY 这个钱包付 Gas 部署。
    python deploy_names.py
收费直接进回购分账合约；官方保留名给收款钱包。部署后把合约地址填进网站 names.js 的 NAMES。
"""
import json
import os

from eth_abi import encode
from eth_account import Account

from chains import send_tx

DAI = "0x83fd06F0846d9D90B3016bF670Efe2E0B11cDe14"
PAYEE = "0x99f47ca8e7ed873e01a337aefc5b75f5328d2da3"  # 回购分账合约
RESERVE = "0xEdb9C6900A11d2dd37Da6B59f3604C35A39DB1e6"  # 收款钱包
RESERVED = ["admin", "official", "kefu", "ailgns", "helix", "awake", "lgns", "glgns", "otc", "support",
            "service", "system", "root", "anubis", "polygon", "team", "awakehelix", "guanfang"]


def main():
    acct = Account.from_key(os.environ["OTC_KEEPER_KEY"])
    art = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "AilgnsNames.json")))
    args = encode(["address", "address", "address", "string[]"], [DAI, PAYEE, RESERVE, RESERVED]).hex()
    h, addr, block = send_tx("anubis", acct, None, art["bytecode"] + args, "部署")
    print(f"部署交易 {h}")
    print(f"域名合约地址 {addr} 区块 {block}")
    print(f"构造参数 {args}")


if __name__ == "__main__":
    main()
