"""邮件通知：用 QQ 邮箱的 SMTP 发信（自己发给自己）。
需要的环境变量（放在 GitHub Secrets，不要写进代码，仓库是公开的）：
  SMTP_USER    发信的 QQ 邮箱，例如 12345@qq.com
  SMTP_PASS    QQ 邮箱的「授权码」（不是 QQ 密码；在 QQ 邮箱 设置 → 账号 → 开启 SMTP 服务 后生成）
  NOTIFY_EMAIL 收通知的邮箱（不填就发给 SMTP_USER 自己）
没设置时只打印、不发送。
"""
import os
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr


def send(subject, body):
    user, pwd = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    to = os.environ.get("NOTIFY_EMAIL") or user
    if not user or not pwd:
        print(f"（没设置 SMTP_USER / SMTP_PASS，邮件跳过）{subject}")
        return False
    host = os.environ.get("SMTP_HOST") or "smtp.qq.com"
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("AiLGNS 服务站", "utf-8")), user))
    msg["To"] = to
    try:
        with smtplib.SMTP_SSL(host, 465, context=ssl.create_default_context(), timeout=30) as s:
            s.login(user, pwd)
            s.sendmail(user, [to], msg.as_string())
        print(f"已发邮件：{subject}")
        return True
    except Exception as e:  # 发信失败不影响其他任务
        print(f"邮件发送失败：{e}")
        return False
