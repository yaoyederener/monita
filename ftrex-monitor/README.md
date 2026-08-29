# FTREX FTR/USDT Monitor

GitHub Actions 每5分钟抓取 FTREX FTR/USDT：

- 20档买盘、卖盘金额与盘口失衡；
- 最新成交的主动买入、主动卖出方向；
- 当日已采集买卖金额；
- Telegram 每小时报告与即时失衡预警（采集仍每5分钟运行）；
- 北京时间跨日后的第一次采集发送上一日成交总结。

工作流复用仓库现有 Secrets：

- `BOT_TOKEN`
- `CHAT_ID`

监控不读取钱包私钥或 FTREX 登录令牌。

手动运行 GitHub Actions 时会强制发送一条每小时监控报告，方便验证 Telegram 通知。

> GitHub Actions 不是连续进程。如果5分钟内成交超过页面“最新成交”列表容量，日统计可能漏掉部分成交；盘口快照和告警不受该限制。
