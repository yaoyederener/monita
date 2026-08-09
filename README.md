# POTS Money 链上监控

这个仓库通过 GitHub Actions 和 Telegram 持续监控 POTS / IBS 在 BNB Smart Chain 上的核心资金与风险。

## 核心指标

`usdt_balance_monitor.py` 汇报以下五组核心数据：

1. **国库资金**
   - `IBS/USDT Pair.getReserves()` 中的 USDT + `USDT.balanceOf(RBS)` + `USDT.balanceOf(Safety Treasury)`
   - 显示总额、三部分明细、24 小时变化和 7 天变化
   - LP 使用交易对储备口径；同时核对代币实际余额，二者不一致时单独告警
   - 按 24 小时和 7 天中较快的净下降速度，显示“国库静态可持续时间”
2. **已知协议地址 USDT 净流入**
   - 对所有已核对 POTS 地址的 `USDT.balanceOf()` 求和，并比较 24h/7d 总余额变化
   - 项目内部互转在总和里天然抵消，不依赖可能被 RPC 限流的历史日志
   - IBS 合约的 `treasury()` 与 `taxTreasury()` 地址会在同一确认区块动态读取；地址边界变化时自动重建基线
   - 显示 24 小时和 7 天净额
3. **IBS 市值与当前总量**
   - 池内现价来自同一确认区块的 IBS/USDT 储备比
   - 流通市值估算 = 池内现价 × 流通量代理值
   - 当前总量直接读取 `IBS.totalSupply()`，同时显示扣除零地址和 dead 地址后的流通量代理值
4. **每日增发与每日抛压**
   - 近 24 小时净增发使用 `IBS.totalSupply()` 的变化，并按当前池内价格显示 USDT 等值
   - 近 24 小时真实抛压来自 IBS/USDT Pair 的 `Swap` 日志，只累计 IBS 净流入池且 USDT 净流出池的卖出
5. **保留的现有风险指标**
   - 继续显示 LP+RBS、RBS 24 小时/7 天变化和 RBS 可支撑天数
   - 可见 USDT 覆盖估算：`(LP USDT 储备 + RBS USDT + Safety USDT) ÷ 流通 IBS`
   - 这是便于观察趋势的链上代理值，不是清算承诺或审计后的资产净值

余额、储备、总量和价格都固定在同一个确认区块。程序还会校验 LP 的 token0/token1 确实是官方 IBS 与目标 USDT，配置错误时会停止而不是发送误报。Swap 日志读取失败时，Telegram 会把抛压显示为“暂时无法读取”，不会误报为零，也不会影响其他指标推送。

## Telegram 判断

报告分为以下状态：

- 🟢 增长阶段：24h/7d 国库资金和已知协议净流入都为正，RBS 与覆盖估算没有恶化
- 🟡 观察阶段：指标方向不一致
- 🟠 下行阶段：24h 国库资金、已知协议净流入和 RBS 同时为负
- 🔴 死亡螺旋风险：24h/7d 持续流出、RBS 7d 跌幅至少 10%、覆盖估算下降且按当前速度可支撑不超过 14 天

这是一套风险信号，不是投资结论。首次升级运行只建立新基线；24h 与 7d 判断会在相应时间后自动出现。

RBS 或 Safety Treasury 相对上次报告的余额下降达到阈值时，会立即发送紧急通知，并同时显示整个已知协议地址边界的净变化，帮助区分内部调动与整体资金流出。

## 运行频率与变量

工作流每 5 分钟检查一次，默认每 60 分钟发一条常规 Telegram 报告。以下情况会提前报告：

- LP + RBS 总额相对上次报告变化达到 100,000 USDT
- 国库资金总额相对上次报告变化达到 100,000 USDT
- RBS / Safety Treasury 余额下降达到阈值

可以在 `Settings → Secrets and variables → Actions → Variables` 调整：

- `POTS_FLOW_REPORT_MINUTES`：常规报告间隔，默认 `60`
- `POTS_FLOW_IMMEDIATE_ALERT_USDT`：核心总额即时报告阈值，默认 `100000`
- `POTS_CRITICAL_OUTFLOW_USDT`：金库大额余额下降阈值，默认 `100000`

## GitHub Secrets

仓库在 `Settings → Secrets and variables → Actions` 中需要：

- `BOT_TOKEN`：Telegram Bot Token
- `CHAT_ID`：Telegram 群 ID
- `BSC_RPC`：BNB Smart Chain Mainnet RPC

监控只读链上数据，不需要钱包私钥。

## 历史记录与升级

- `data/usdt_balance.json`：最新报告基线及最近 14 天快照，包含国库、价格、市值和总量字段
- `data/usdt_flow_history.csv`：继续保留每次报告的原有指标列，兼容既有历史
- Git 提交历史：每次有效报告后的状态都会提交，旧的 RBS 单地址记录仍可追溯

升级时会保留旧 `raw_balance`、区块和时间信息，并建立新的四指标基线，不会把未知的历史 LP 数量拼进旧记录硬算涨跌。

## 手动启动

进入 `Actions → POTS Core Funds Monitor → Run workflow`。

## 指标边界

- LP 储备会受普通用户买卖影响，所以“国库资金增长”不等于经营利润。
- “国库静态可持续时间”把 LP、RBS 和 Safety 合计余额按近期净下降速度外推，只用于趋势预警；LP 不是可直接支出的运营现金，Safety 也可能受权限限制。
- 市值使用单个确认区块的池内现价估算，可能受短时交易影响，不是 TWAP、审计估值或实际可退出价值。
- “真实抛压”是近 24 小时卖入该 LP 的总量，不扣除同期买盘，因此不是净资金流。
- 已知协议净流入只覆盖脚本中的地址和 IBS 当前 treasury 角色；项目启用其他新合约后仍应同步更新静态地址集合。
- 可见 USDT 覆盖估算只纳入 LP 的 USDT 储备、RBS 与 Safety，不代表所有资产、债务、LP 所有权或可立即兑现价值。
- runway 假设近期消耗速度继续，不是精确预测；RBS 停止下降时显示为“未持续消耗”。

## 其他现有监控

- `monitor.py`：IBS 大额交易通知
- `pots_risk_monitor.py`：LP、RBS、金库、Mint/Burn、买卖压力和合约变化风险监控
- `README-POTS-RISK.md`：POTS 风险监控详细说明

这些监控继续共存，不会因为本监控升级而删除。

## 官方地址与原理

- [POTS 官方合约表](https://potsdefi.com/resources/contracts/)
- [POTS Smart Treasury](https://potsdefi.com/money/smart-treasury/)
- [IBS/USDT LP](https://bscscan.com/address/0x2a4b99a9c4544d35e8d266111c50b67fea01d53d)
- [RBS Stabilizer](https://bscscan.com/address/0xCBA922f6aff0EC8CB0703D44249456Ef779A394C)
