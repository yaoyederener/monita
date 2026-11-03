"""部署域名系统第 3 版（Anubis 链，一次）：.ailgns、.lgns 两个域名合约（支持中文，可用 DAI / LGNS / AiLGNS 付），再各配一个域名市场（AWAKE NFT 托管合约同一份代码）。
    python deploy_names_otc.py
注册费、续费、市场 3% 手续费全部进回购分账合约；保留名给收款钱包。部署后把 4 个地址填进网站 names.js。
"""
import json
import os

from eth_abi import encode
from eth_account import Account

from chains import send_tx

DAI = "0x83fd06F0846d9D90B3016bF670Efe2E0B11cDe14"
# 也收 LGNS、AiLGNS，按 DEX 现价换算：[DAI, LGNS, LGNS/DAI 交易对, AiLGNS, AiLGNS/DAI 交易对]
TOKENS = [DAI, "0x4D1D808a081FdAc440703b3765FC61f8028C06B8", "0x32A4586797E3f561F41C0F47CA57eD08D64f3dC0",
          "0x256EdB652db43E721EC6A12B6b51b6967D3bb8cB", "0x0a1Cce65d995C6C5100e6F5bA3c388Ef34CEcaf0"]
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
        args = encode(["address[5]", "address", "address", "string[]"], [TOKENS, PAYEE, RESERVE, RESERVED]).hex()
        h, names, block = send_tx("anubis", acct, None, art(file)["bytecode"] + args, "部署")
        print(f".{tld} 域名合约地址 {names} 区块 {block} 交易 {h}")
        args = encode(["address", "address", "address"], [names, DAI, PAYEE]).hex()
        h, otc, block = send_tx("anubis", acct, None, esc["bytecode"] + args, "部署")
        print(f".{tld} 域名市场合约地址 {otc} 区块 {block} 交易 {h}")


if __name__ == "__main__":
    main()
