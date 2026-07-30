IBS监控（Alchemy BSC版）

上传到GitHub仓库：
1. monitor.py -> 仓库根目录
2. requirements.txt -> 仓库根目录
3. mint_origin_addresses.json -> 仓库根目录
4. monitor.yml -> .github/workflows/monitor.yml

GitHub Secrets必须存在：
- BOT_TOKEN
- CHAT_ID
- BSC_RPC

BSC_RPC填写你的Alchemy BNB Mainnet RPC。
不要把完整Alchemy Key直接写进公开仓库。

运行日志会显示Alchemy Key末4位，用于确认实际加载的是你的RPC。
