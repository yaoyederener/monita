"""Anubis 和 Polygon 两条链的节点调用（分账、部署共用）"""
import json
import os
import time
import urllib.request

from ailgns_lp_monitor import rpc as anubis_rpc, LOCAL_RPCS, _LOCAL_DOWN

POLYGON_RPCS = ["https://polygon-bor-rpc.publicnode.com", "https://polygon.drpc.org", "https://polygon-rpc.com"]
CHAIN_IDS = {"anubis": 6714, "polygon": 137}


def polygon_rpc(method, params):
    last = None
    for attempt in range(6):
        try:
            req = urllib.request.Request(POLYGON_RPCS[attempt % len(POLYGON_RPCS)],
                                         json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                                         {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 ailgns-monitor"})
            r = json.load(urllib.request.urlopen(req, timeout=60))
            if "error" in r:
                raise RuntimeError(r["error"])
            return r["result"]
        except Exception as e:  # 换一个节点重试
            last = e
            time.sleep(1)
    raise last


def rpc(chain, method, params):
    return anubis_rpc(method, params) if chain == "anubis" else polygon_rpc(method, params)


def send_tx(chain, acct, to, data, label, nonce=None):
    """签名并发送交易，等确认后返回交易哈希；to=None 表示部署合约，返回 (哈希, 合约地址)。
    nonce：调用方自己定交易编号（极速发奖：发之前现查过链上、编号对得上才发，见 fast_oracle.pay_out）；不给就用节点给的下一个"""
    from eth_utils import to_checksum_address
    tx = {"from": acct.address, "data": data}
    if to:
        tx["to"] = to
    gas = int(int(rpc(chain, "eth_estimateGas", [tx]), 16) * 1.3)
    price = int(rpc(chain, "eth_gasPrice", []), 16)
    if chain == "polygon":
        price = int(price * 1.2)  # Polygon 拥堵时稍微多给一点，避免卡住
    if nonce is None:
        nonce = int(rpc(chain, "eth_getTransactionCount", [acct.address, "pending"]), 16)
    body = {"data": data, "value": 0, "chainId": CHAIN_IDS[chain], "gas": gas, "gasPrice": price, "nonce": nonce}
    if to:
        body["to"] = to_checksum_address(to)
    raw = acct.sign_transaction(body).raw_transaction.hex()
    h = rpc(chain, "eth_sendRawTransaction", [raw if raw.startswith("0x") else "0x" + raw])
    poll = float(os.environ.get("TX_POLL") or 2)  # 等确认时多久查一次：常驻发奖程序设 0.5（这条链约 1 秒一个块，2026-11-28）；其余照旧 2 秒
    # 「编号已被别的交易用掉」就提前判作废（2026-11-28：节点刚重启、读到的编号是旧的，白等了 3 分钟）。只有设了 TX_FAILFAST=1 才开：
    # 站长服务器上的常驻发奖程序开（install.sh 写进 .env，它只连本机节点）；GitHub 上的机器人不开，照旧等满 3 分钟——公共节点可能几台轮流应答，
    # 一台看到上链、另一台还没看到时会像「编号用掉了却没有收据」，误判作废的话有的机器人会重发、发两遍。
    # 编号按顺序用：链上已发数 > 这笔的编号 = 这个编号已经上链；连着 30 秒都查不到这笔的收据，才说明上链的不是它
    # 只在「正在用本机节点」的时候才提前判（复查 2026-11-28）：本机节点连不上、临时改走公共节点的那一段，照旧等满 3 分钟
    failfast = os.environ.get("TX_FAILFAST") == "1"
    on_local = lambda: chain == "anubis" and bool(LOCAL_RPCS) and not _LOCAL_DOWN[0]
    every, gone = max(1, int(10 / poll)), None  # gone：第一次看到「编号已用掉、却没有这笔收据」的时间
    for i in range(int(180 / poll)):
        r = rpc(chain, "eth_getTransactionReceipt", [h])
        if r:
            if r["status"] != "0x1":
                raise RuntimeError(f"{label}失败: {h}")
            return (h, r["contractAddress"], int(r["blockNumber"], 16)) if not to else h
        if failfast and i and i % every == 0:
            if not on_local():
                gone = None  # 这会儿在用公共节点：不算，重新计时
            elif int(rpc(chain, "eth_getTransactionCount", [acct.address, "latest"]), 16) > nonce:
                gone = gone or time.time()
                if time.time() - gone >= 30:
                    raise RuntimeError(f"{label}作废：交易编号 {nonce} 已被别的交易用掉（多半是节点刚重启、编号读旧了）: {h}")
            else:
                gone = None
        time.sleep(poll)
    raise RuntimeError(f"{label} 3 分钟未确认: {h}")
