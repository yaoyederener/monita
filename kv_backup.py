"""每天备份一次 ailgns.dev 的 KV 数据（会员开通记录、VIP 转让、代开绑定、像素墙、团队树：这些链上恢复不了），
打包成 .json.gz 附件发到 NOTIFY_EMAIL。仓库是公开的，备份文件不进仓库，只在 data/kv_backup.json 记最后备份的日期。
需要的环境变量：BACKUP_KEY（和 Cloudflare Pages 里的 BACKUP_KEY 相同），SMTP_USER / SMTP_PASS / NOTIFY_EMAIL
    python kv_backup.py          # 今天（北京时间）还没备份就备份
    python kv_backup.py --force  # 立刻备份一次
恢复：解压附件得到 [[key, value], …]，逐条写回 Cloudflare KV（TEAM_KV）即可。
"""
import argparse
import gzip
import json
import os
import smtplib
import ssl
import time
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

URL = "https://ailgns.dev/api/kv-backup"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "kv_backup.json")


def dump(key):
    items, cursor = [], ""
    while cursor is not None:
        req = urllib.request.Request(URL + "?cursor=" + urllib.parse.quote(cursor), headers={"Authorization": "Bearer " + key, "User-Agent": "monita-kv-backup"})
        j = json.load(urllib.request.urlopen(req, timeout=60))
        items += j["items"]
        cursor = j["cursor"]
    return items


def mail(subject, body, name, blob):
    user, pwd = os.environ["SMTP_USER"], os.environ["SMTP_PASS"]
    to = [os.environ.get("NOTIFY_EMAIL") or user]
    msg = MIMEMultipart()
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("AiLGNS 服务站", "utf-8")), user))
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(body, "plain", "utf-8"))
    part = MIMEApplication(blob, Name=name)
    part["Content-Disposition"] = f'attachment; filename="{name}"'
    msg.attach(part)
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST") or "smtp.qq.com", 465, context=ssl.create_default_context(), timeout=60) as s:
        s.login(user, pwd)
        s.sendmail(user, to, msg.as_string())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    key = os.environ.get("BACKUP_KEY")
    if not key or not os.environ.get("SMTP_USER") or not os.environ.get("SMTP_PASS"):
        print("没设置 BACKUP_KEY / SMTP_USER / SMTP_PASS，跳过 KV 备份")
        return
    day = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))  # 北京时间的日期
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    if state.get("day") == day and not args.force:
        return
    items = dump(key)
    count = {}
    for k, _ in items:
        p = k.split(":")[0]
        count[p] = count.get(p, 0) + 1
    blob = gzip.compress(json.dumps(items, ensure_ascii=False).encode())
    name = f"ailgns-kv-{day}.json.gz"
    summary = "，".join(f"{p} {n} 条" for p, n in sorted(count.items()))
    mail(f"AiLGNS KV 备份 {day}", f"共 {len(items)} 条：{summary}\n附件解压后是 [[key, value], …]，逐条写回 Cloudflare KV（TEAM_KV）即可恢复。\n\n—— AiLGNS 服务站", name, blob)
    print(f"KV 备份已发邮件：{len(items)} 条，{len(blob) / 1024:.1f} KB（{summary}）")
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump({"day": day, "count": len(items)}, f)


if __name__ == "__main__":
    main()
