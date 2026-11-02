# FLGNS OTC

Anubis（6714）上的独立 FLGNS 场外托管交易网站。四个市场：FLGNS/DAI、FLGNS/LGNS、FLGNS/AiLGNS、FLGNS/RM。界面采用独立青绿配色，仅提供 OTC。

新版每次成交，挂单方和吃单方各从自己收到的资产中扣除 0.4%；两种代币各自的费用，60% 直接进入 `0xd07Fe622d0D1628780853b31C21B9ABAf96B9c00`，40% 进入原收款钱包 `0xedb9c6900a11d2dd37da6b59f3604c35a39db1e6`，后者沿用原 40%/30%/30% 分账。

新合约沿用原 OTC 的托管、原子成交、部分成交、撤单及到期退款逻辑，并核对每笔转出实际扣款与实际到账。地址和费率固定，没有管理员、升级或任意提款入口。外部代币本身仍有各自的权限和兼容性限制。

网页只有在配置了经过链上核对的合约地址后才能交易。`dist/config.js` 中 `deploymentReady` 为 false 时会明确禁用交易。`split-setup.html` 是收款钱包的独立运维授权页，主导航不显示。

静态入口为 `dist/index.html`；无需构建或第三方 JavaScript CDN。正式托管已迁至用户 Cloudflare 账户的 `flgns-otc` Static Assets Worker，绑定 `flgns.org`；`.openai/hosting.json` 仅保留原 Sites 项目标识，不是现有生产发布目标。新版合约及 ABI 见 `contracts/v2/`，部署记录与兼容旧/新的退款、分账源码保存在原机器人仓库的 `flgns/bilateral/` 目录。

原不可变 2% 合约保留，`dist/legacy/` 只提供查询、撤单及到期退款，不提供新挂单、成交或授权。旧订单不迁移资产，新挂单使用 `dist/config.js` 中四个经过链上验证的 V2 合约。切换准备及独立证据见工作目录 `outputs/flgns-bilateral-preflight/STATUS.md`。
