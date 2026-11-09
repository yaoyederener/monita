"""预言家预测上链存证（站长 2026-11-07 定）
每天北京时间 21:00 封卷后：从网站拿本期（今天）的规范清单和 SHA-256 指纹 → 自己再算一遍核对 → 奖池钱包给自己发一笔 0 元交易，
data = "ailgns-oracle:<日期>:<指纹>" → 把交易哈希回写给网站（POST /api/oracle action=anchor，带 BACKUP_KEY）。
只做普通转账，不碰任何合约管理权限。一天只写一次；没有预测的那天不写。
需要：POOL_KEY（奖池钱包）、BACKUP_KEY。
"""
import hashlib
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

from eth_account import Account

from chains import send_tx

SITE = "https://ailgns.dev"
BJ = timezone(timedelta(hours=8))
DRAW_H = 21


def http(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json", "user-agent": "AiLGNS-keeper/oracle-anchor"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    pool_key, backup = os.environ.get("POOL_KEY"), os.environ.get("BACKUP_KEY")
    if not pool_key or not backup:
        print("oracle_anchor: 缺 POOL_KEY / BACKUP_KEY，跳过"); return
    now = datetime.now(BJ)
    day = now.strftime("%Y-%m-%d")
    cut_h = 9 if day >= "2026-11-10" else DRAW_H  # 站长 2026-11-09：11 月 10 日起早 9 点封卷（锁定 12 小时），封卷一到就写指纹
    if now.hour < cut_h or (now.hour == cut_h and now.minute < 1):
        print(f"oracle_anchor: {day} 还没到 {cut_h}:00 封卷，跳过"); return
    p = http(f"{SITE}/api/oracle?proof={day}")
    if p.get("anchor"):
        print(f"oracle_anchor: {day} 已存证 {p['anchor']['tx']}"); return
    if not p.get("n"):
        print(f"oracle_anchor: {day} 没有预测，不写"); return
    local = hashlib.sha256(p["canon"].encode("utf-8")).hexdigest()
    if local != p["hash"]:
        print(f"oracle_anchor: 指纹不一致 本地 {local} 服务器 {p['hash']}，不写"); sys.exit(1)
    acct = Account.from_key(pool_key)
    data = "0x" + f"ailgns-oracle:{day}:{local}".encode("utf-8").hex()
    tx = send_tx("anubis", acct, acct.address, data, f"预言家存证 {day}")
    print(f"oracle_anchor: {day} {p['n']} 人 {p['guesses']} 个预测 → 链上 {tx}")
    r = http(f"{SITE}/api/oracle", {"action": "anchor", "key": backup, "day": day, "tx": tx, "hash": local})
    print("oracle_anchor: 回写", r)


if __name__ == "__main__":
    main()
