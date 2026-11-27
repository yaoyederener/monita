"""预言家预测上链存证（站长 2026-11-07 定）
每天封卷后（11-12 起北京时间下午 3 点封卷，封卷满 16 分钟再写，见 anchor_time）：从网站拿本期（今天）的规范清单和 SHA-256 指纹 → 自己再算一遍核对 → 奖池钱包给自己发一笔 0 元交易，
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


def anchor_time(day):
    """哪天几点写指纹（北京时间，时, 分）。封卷时间和网站 lib/oracle.js 的 CUT_FROM / CUT_FROM2 / CUT_H 一致：11-10 起早上 9 点，11-12 起下午 3 点。
    站长 2026-11-28：这里以前一直停在早上 9 点；网站 11-12 改成下午 3 点封卷以后，链上指纹只盖住了 9 点前交的那一小部分（11-27 那期 542 人里只有 65 人），
    页面上「本期全部预测已写入链上」名不副实，存证卡片上「本期指纹」和「链上指纹」也对不上。开奖、发奖从来不看存证，没影响到钱。
    现在等封卷满 16 分钟再写：网站封卷满 15 分钟会把当天的预测打包成一条（lib/oracle.js 的 sealedGuesses），
    这里拿的清单、写完回报时网站的核对、页面上的存证卡片读的是同一份，指纹一定对得上；KV 各地同步也有足够时间"""
    if day >= "2026-11-12":
        return 15, 16
    if day >= "2026-11-10":
        return 9, 1
    return DRAW_H, 1


def main():
    pool_key, backup = os.environ.get("POOL_KEY"), os.environ.get("BACKUP_KEY")
    if not pool_key or not backup:
        print("oracle_anchor: 缺 POOL_KEY / BACKUP_KEY，跳过"); return
    now = datetime.now(BJ)
    day = now.strftime("%Y-%m-%d")
    cut_h, cut_m = anchor_time(day)
    if (now.hour, now.minute) < (cut_h, cut_m):
        print(f"oracle_anchor: {day} 还没到 {cut_h}:{cut_m:02d}（封卷之后）写指纹的时间，跳过"); return
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
