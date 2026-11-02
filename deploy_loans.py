"""部署 LGNS、gLGNS 抵押借款合约（Anubis 链，一次）。和 AiLGNS 借款合约是同一份源码（AilgnsLoanEscrow），只是抵押币不同。
用 OTC_KEEPER_KEY 这个钱包付 Gas 部署（部署钱包在合约里没有任何权限）。
    python deploy_loans.py
部署后把合约地址和区块填进网站 lend.js、book.js、loan-watch.js，以及本仓库 otc_refund_keeper.py。
"""
import json
import os

from eth_account import Account

from chains import send_tx

DAI = "0x83fd06F0846d9D90B3016bF670Efe2E0B11cDe14"
FEE = "0xedb9c6900a11d2dd37da6b59f3604c35a39db1e6"  # 手续费收款（和其他合约相同）
COLLATERALS = [("LGNS", "0x4D1D808a081FdAc440703b3765FC61f8028C06B8"), ("gLGNS", "0xf140a92c156e76f5a1d3d6f96284b250330f8f4c")]


def word(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def main():
    acct = Account.from_key(os.environ["OTC_KEEPER_KEY"])
    art = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "AilgnsLoanEscrow.json")))
    for sym, token in COLLATERALS:
        data = art["bytecode"] + word(token) + word(DAI) + word(FEE)  # constructor(collateral, dai, feeRecipient)
        h, addr, block = send_tx("anubis", acct, None, data, f"部署 {sym} 借款合约")
        print(f"{sym} 抵押借款合约 {addr} 区块 {block} 部署交易 {h}")


if __name__ == "__main__":
    main()
