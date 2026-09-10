# FTREX FTR/USDT Monitor (Paused)

The scheduled GitHub Actions market/trade monitor is paused. It has been replaced
operationally by the separate Cloudflare BSC-USDT deposit/withdrawal funds monitor;
the historical implementation is retained here for reference only.

GitHub Actions 计划任务抓取 FTREX FTR/USDT：

- 买一卖一、价差和 0.5%/1%/2% 范围内的双边深度；
- 5/15/60 分钟价格变化和主动成交净额；
- 以 1,000 USDT 模拟的买卖滑点；
- 最近 1 小时最大成交与数据新鲜度；
- Telegram 每小时报告、分级预警和监控故障通知；
- 北京时间跨日后的第一次采集发送上一日成交总结。

盘口方向必须连续 3 次出现，并至少得到主动成交、价格变化、近端深度下降或价差扩大中的一项交叉确认，才会发送带 `@juzhangniubi666` 的高优先级通知。普通观察提醒和常规报告不 @。

成交窗口按交易所显示的实际成交时刻计算，不再按 GitHub 抓取时刻计算。如果两次成功采集间隔超过 20 分钟，本轮会标记数据不连续、停止方向性结论并 @ 管理员。连续抓取失败 3 次也会在 Telegram 报警，恢复后发送恢复通知。

工作流复用仓库现有 Secrets：

- `BOT_TOKEN`
- `CHAT_ID`

监控不读取钱包私钥或 FTREX 登录令牌。

手动运行 GitHub Actions 时会强制发送一条每小时监控报告，方便验证 Telegram 通知。

> GitHub Actions 不是连续进程。如果5分钟内成交超过页面“最新成交”列表容量，日统计可能漏掉部分成交；盘口快照和告警不受该限制。
