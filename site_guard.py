"""网站防篡改监控：每 5 分钟读一遍 ailgns.dev 线上的首页和全部脚本，
1. 首页加载了外站脚本 → 报警（本站只该加载自己的脚本）
2. 脚本里出现了白名单（data/site_guard_allow.json）以外的合约 / 钱包地址 → 报警
   黑客拿到 GitHub 或 Cloudflare 后，最可能做的就是把网页里的合约地址换成自己的，骗用户授权
白名单放在 monita 仓库，和网站仓库分开：网站被改了，白名单不会跟着变。
站长自己往网站里加了新地址（比如手动开通会员），把新地址加进白名单就不会再报。
同一批可疑地址只报一次（记在 data/site_guard.json）。
需要的环境变量：SMTP_USER / SMTP_PASS / NOTIFY_EMAIL
"""
import json
import os
import re
import urllib.request

from notify_mail import send as send_mail

SITE = "https://ailgns.dev/"
RAW = "https://raw.githubusercontent.com/yaoyederener/ailgns-data/main/"
HERE = os.path.dirname(os.path.abspath(__file__))
ALLOW = os.path.join(HERE, "data", "site_guard_allow.json")
STATE = os.path.join(HERE, "data", "site_guard.json")
TRUSTED = ("https://static.cloudflareinsights.com/",)  # Cloudflare 网站统计自动插入的脚本
ADDR = re.compile(r"0x[0-9a-fA-F]{40}(?![0-9a-fA-F])")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "monita-site-guard", "Cache-Control": "no-cache"})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def main():
    html = get(SITE + "?guard")
    srcs = re.findall(r'<script[^>]*\bsrc="([^"]+)"', html)
    problems = [f"首页加载了外站脚本：{s}" for s in srcs if re.match(r"(https?:)?//", s) and not s.startswith(TRUSTED)]
    allow = set(json.load(open(ALLOW)))
    found = {}
    for s in srcs:
        if re.match(r"(https?:)?//", s):
            continue
        for a in ADDR.findall(get(SITE + s)):
            found.setdefault(a.lower(), set()).add(s.split("?")[0])
    for a in ADDR.findall(html):
        found.setdefault(a.lower(), set()).add("index.html")
    new = sorted(a for a in found if a not in allow)
    if new:
        # 对照一下 GitHub 上的源码：GitHub 里也有，多半是站长自己改的；GitHub 里没有，说明是在 Cloudflare 上被改的，非常危险
        repo = ""
        for f in {f for a in new for f in found[a]}:
            try:
                repo += get(RAW + f)
            except Exception:
                pass
        repo = repo.lower()
        for a in new:
            where = "，".join(sorted(found[a]))
            problems.append(f"{a}（在 {where}）" + ("　GitHub 源码里也有：如果是你刚改的网站，把它加进白名单即可" if a in repo else "　⚠️ GitHub 源码里没有：线上网站可能被篡改！"))
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    key = "|".join(problems)
    if not problems:
        print(f"网站检查正常：{len(srcs)} 个脚本，{len(found)} 个地址都在白名单里")
        if state.get("alerted"):
            _save({})
        return
    print("网站检查发现问题：\n" + "\n".join(problems))
    if state.get("alerted") == key:
        return
    send_mail("⚠️ AiLGNS 网站防篡改报警", "线上 ailgns.dev 发现以下问题：\n\n" + "\n".join(problems)
              + "\n\n如果不是你自己改的：立刻检查 GitHub 和 Cloudflare 的登录记录，并在群里提醒用户暂停授权和交易。\n\n—— AiLGNS 服务站")
    _save({"alerted": key})


def _save(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(state, f)


if __name__ == "__main__":
    main()
