#!/bin/bash
# 在服务器上装做市买单常驻版（bid_daemon.py）。只装程序，不启动；启动要先用 bidbot-setkey 放私钥。
# 用法（在本机 monita 目录）：bash server/bidbot-install.sh root@服务器IP
set -euo pipefail
HOST="$1"
ssh "$HOST" 'mkdir -p /opt/bidbot /var/lib/bidbot && chmod 700 /var/lib/bidbot'
scp -q bid_daemon.py bid_maker.py chains.py ailgns_lp_monitor.py server/bidbot-setkey "$HOST":/opt/bidbot/
ssh "$HOST" 'set -e
cd /opt/bidbot
chmod 700 bidbot-setkey && ln -sf /opt/bidbot/bidbot-setkey /usr/local/bin/bidbot-setkey
[ -x venv/bin/python ] || { apt-get install -y -q python3-venv >/dev/null; python3 -m venv venv; }
venv/bin/pip install -q "eth-account==0.13.4"
cat > /etc/systemd/system/bidbot.service <<UNIT
[Unit]
Description=AiLGNS bid maker (resident)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/bidbot
EnvironmentFile=/etc/bidbot.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/bidbot/venv/bin/python -u bid_daemon.py
Restart=always
RestartSec=5
MemoryMax=300M
CPUQuota=25%

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
venv/bin/python -c "import bid_daemon" 2>&1 | tail -1 || true
echo "装好了（没启动）。下一步：ssh -t '"$HOST"' bidbot-setkey"'
