# POTS / IBS 撤退风险 Telegram 监控

这套监控与现有 `monitor.py` 共存：

- 现有脚本：继续通知 IBS 大额买卖。
- 新脚本 `pots_risk_monitor.py`：判断项目是否进入“停止复利、回本、减仓、紧急撤退”阶段。

## 已监控指标

1. IBS/USDT 池价格和 USDT 储备。
2. RBS、Safety Treasury、Release Turbine、Bonding 的 USDT 余额。
3. RBS 24 小时下降比例和按当前消耗速度估算的可支撑天数。
4. IBS 每日/7日 Mint、Burn 和净通胀率。
5. 每日总买入、卖出、外部买入、可识别协议买入占比。
6. RBS/LP USDT 比率代理。
7. RBS 或 Safety Treasury 向陌生地址的大额转账。
8. 核心合约 bytecode、EIP-1967 implementation、owner 变化。
9. 每天 20:00（Vancouver 时间）发送风险日报。

## 默认行动等级

- 绿色：没有触发撤退线。
- 黄色：停止新增和复利，开始回收本金。
- 红色：优先回本，考虑撤出 50%—80%。
- 黑色：优先紧急退出，不等待线性释放。

这些是风险管理阈值，不是收益保证或自动交易指令。

## 放入现有仓库

把下列文件放进 `yaoyederener/monita`：

```text
pots_risk_monitor.py
requirements-pots-risk.txt
data/pots_risk_state.json
.github/workflows/pots-risk-monitor.yml
```

不要删除现有 `monitor.py` 和原来的 workflow。

## GitHub Secrets

仓库进入：

`Settings → Secrets and variables → Actions`

继续使用已有的：

- `BOT_TOKEN`：现有 Telegram Bot Token
- `CHAT_ID`：现有群 ID `-1004369632577`
- `BSC_RPC`：现有 Alchemy BSC Mainnet RPC

Bot 仍然使用现有 `@Ibssellbot`。Token 不要写进代码或公开仓库。

## 启动

1. GitHub 打开 `Actions`。
2. 选择 `POTS Exit Risk Monitor`。
3. 点击 `Run workflow`。
4. 首次运行应收到“POTS 撤退风险监控已启动”。
5. 首次运行只建立当前基准，不补发大量历史记录。

## 默认阈值

可以在 workflow 的 `env` 或仓库 Variables 中覆盖：

| 变量 | 默认值 | 含义 |
|---|---:|---|
| `SELL_BUY_YELLOW` | 1.20 | 卖出/外部买入黄色线 |
| `SELL_BUY_RED` | 1.50 | 卖出/外部买入红色线 |
| `RBS_24H_DROP_YELLOW` | 0.10 | RBS 24h下降10% |
| `RBS_24H_DROP_RED` | 0.20 | RBS 24h下降20% |
| `LP_24H_DROP_YELLOW` | 0.05 | LP USDT 24h下降5% |
| `LP_7D_DROP_RED` | 0.10 | LP USDT 7d下降10% |
| `NET_INFLATION_7D_YELLOW` | 0.05 | 7日净通胀5% |
| `NET_INFLATION_7D_RED` | 0.10 | 7日净通胀10% |
| `FINGERPRINT_INTERVAL_MINUTES` | 60 | 核心合约权限/实现检查间隔 |
| `UNKNOWN_TREASURY_OUTFLOW_USDT` | 25000 | 金库向陌生地址转出预警线 |
| `LARGE_TREASURY_OUTFLOW_USDT` | 100000 | 金库向陌生地址转出黑色线 |

## 重要限制

- “协议买盘”通过已知合约地址和交易发起地址归因，属于保守估计。
- “LP支持价值”使用 `2 × 池内USDT ÷ IBS总供应量` 作为代理，不等于项目内部精确公式。
- GitHub Actions 的 5 分钟 cron 可能延迟几分钟，不是毫秒级实时系统。
- 如果同一检查周期内资金先转出又转回，交易日志仍会被记录，不只是看最终余额。
