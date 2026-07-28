# BSC USDT Balance Monitor

监控地址：

`0xCBA922f6aff0EC8CB0703D44249456Ef779A394C`

监控代币：

`0x55d398326f99059fF775485246999027B3197955`

## GitHub Secrets

在仓库进入：

`Settings → Secrets and variables → Actions → New repository secret`

添加：

- `BOT_TOKEN`：Telegram Bot Token
- `CHAT_ID`：Telegram 群 ID
- `BSC_RPC`：BSC RPC 地址，例如 Alchemy BNB Mainnet RPC

## 启动

进入：

`Actions → BSC USDT Balance Monitor → Run workflow`

首次运行会向 Telegram 群发送当前余额，并写入
`data/usdt_balance.json`。以后余额发生变化才会通知。

## 注意

这是余额快照监控，每 5 分钟检查一次。如果同一检查周期内先转入、再转出，
且最终余额完全相同，则不会产生余额变化通知。

## 与现有 IBS 监控共存

本项目使用独立文件名：

- `usdt_balance_monitor.py`
- `requirements-usdt.txt`
- `.github/workflows/usdt-balance-monitor.yml`

因此可以直接加入已有仓库，不会覆盖原来的 `monitor.py`。
