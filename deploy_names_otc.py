"""部署域名系统第 2 版（Anubis 链，一次）：.ailgns、.lgns 两个域名合约（支持中文），再各配一个域名市场（AWAKE NFT 托管合约同一份代码）。
    python deploy_names_otc.py
注册费、续费、市场 3% 手续费全部进回购分账合约；保留名给收款钱包。部署后把 4 个地址填进网站 names.js。
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
            "service", "system", "root", "anubis", "polygon", "team", "awakehelix", "guanfang",
            "官方客服", "官方账号", "客服中心"]
HERE = os.path.dirname(os.path.abspath(__file__))


def art(name):
    return json.load(open(os.path.join(HERE, name)))


def main():
    acct = Account.from_key(os.environ["OTC_KEEPER_KEY"])
    esc = art("NftEscrow.json")
    for tld, file in (("ailgns", "AilgnsNames.json"), ("lgns", "LgnsNames.json")):
        args = encode(["address", "address", "address", "string[]"], [DAI, PAYEE, RESERVE, RESERVED]).hex()
        h, names, block = send_tx("anubis", acct, None, art(file)["bytecode"] + args, "部署")
        print(f".{tld} 域名合约地址 {names} 区块 {block} 交易 {h}")
        args = encode(["address", "address", "address"], [names, DAI, PAYEE]).hex()
        h, otc, block = send_tx("anubis", acct, None, esc["bytecode"] + args, "部署")
        print(f".{tld} 域名市场合约地址 {otc} 区块 {block} 交易 {h}")


if __name__ == "__main__":
    main()
