"""部署 AiLGNS OTC 批量扫单合约（Anubis 链，一次）。没有构造参数、没有管理员，用 OTC_KEEPER_KEY 这个钱包付 Gas。
    python deploy_router.py
部署后把合约地址填进 ailgns-data/otc.js 的 ROUTER。
"""
import json
import os

from eth_account import Account

from chains import send_tx


def main():
    acct = Account.from_key(os.environ["OTC_KEEPER_KEY"])
    art = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "AilgnsSweepRouter.json")))
    h, addr, block = send_tx("anubis", acct, None, art["bytecode"], "部署")
    print(f"部署交易 {h}")
    print(f"扫单合约地址 {addr} 区块 {block}")


if __name__ == "__main__":
    main()
