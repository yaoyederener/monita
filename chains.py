"""Anubis 和 Polygon 两条链的节点调用（分账、部署共用）"""
import json
import time
import urllib.request

from ailgns_lp_monitor import rpc as anubis_rpc

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


def send_tx(chain, acct, to, data, label):
    """签名并发送交易，等确认后返回交易哈希；to=None 表示部署合约，返回 (哈希, 合约地址)"""
    from eth_utils import to_checksum_address
    tx = {"from": acct.address, "data": data}
    if to:
        tx["to"] = to
    gas = int(int(rpc(chain, "eth_estimateGas", [tx]), 16) * 1.3)
    price = int(rpc(chain, "eth_gasPrice", []), 16)
    if chain == "polygon":
        price = int(price * 1.2)  # Polygon 拥堵时稍微多给一点，避免卡住
    body = {"data": data, "value": 0, "chainId": CHAIN_IDS[chain], "gas": gas, "gasPrice": price,
            "nonce": int(rpc(chain, "eth_getTransactionCount", [acct.address, "pending"]), 16)}
    if to:
        body["to"] = to_checksum_address(to)
    raw = acct.sign_transaction(body).raw_transaction.hex()
    h = rpc(chain, "eth_sendRawTransaction", [raw if raw.startswith("0x") else "0x" + raw])
    for _ in range(90):
        r = rpc(chain, "eth_getTransactionReceipt", [h])
        if r:
            if r["status"] != "0x1":
                raise RuntimeError(f"{label}失败: {h}")
            return (h, r["contractAddress"], int(r["blockNumber"], 16)) if not to else h
        time.sleep(2)
    raise RuntimeError(f"{label} 3 分钟未确认: {h}")
