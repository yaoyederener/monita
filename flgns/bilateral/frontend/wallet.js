/* EIP-6963 and legacy injected wallets. All transactions stay in the user's wallet. */
(function () {
  "use strict";
  var announced = [], current = null, accountListeners = [], chainListeners = [], key = "flgns-wallet-v1";
  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  window.addEventListener("eip6963:announceProvider", function (event) {
    var d = event.detail;
    if (d && d.info && d.provider && typeof d.provider.request === "function" && !announced.some(function (x) { return x.info.uuid === d.info.uuid; })) announced.push(d);
  });
  window.dispatchEvent(new Event("eip6963:requestProvider"));
  function list() {
    var out = announced.map(function (d) { return { id: d.info.rdns || d.info.uuid, name: d.info.name, icon: d.info.icon, provider: d.provider }; });
    function add(name, p) {
      if (!p || typeof p.request !== "function" || out.some(function (w) { return w.provider === p; })) return;
      out.push({ id: name.toLowerCase().replace(/\s/g, ""), name: name, provider: p });
    }
    add("OKX Wallet", window.okxwallet); add("Bitget Wallet", window.bitkeep && window.bitkeep.ethereum);
    add("Trust Wallet", window.trustwallet && (window.trustwallet.ethereum || window.trustwallet));
    add("Coinbase Wallet", window.coinbaseWalletExtension);
    var eth = window.ethereum;
    (eth && eth.providers && eth.providers.length ? eth.providers : eth ? [eth] : []).forEach(function (p) {
      add(p.isOkxWallet || p.isOKExWallet ? "OKX Wallet" : p.isBitKeep || p.isBitgetWallet ? "Bitget Wallet" : p.isTokenPocket ? "TokenPocket" : p.isImToken ? "imToken" : p.isTrust || p.isTrustWallet ? "Trust Wallet" : p.isRabby ? "Rabby" : p.isCoinbaseWallet ? "Coinbase Wallet" : p.isMetaMask ? "MetaMask" : "浏览器钱包", p);
    });
    return out;
  }
  function saved() { try { return localStorage.getItem(key); } catch (_) { return null; } }
  function use(w) {
    if (current && current.provider === w.provider) return w.provider;
    if (current && current.provider.removeListener) {
      current.provider.removeListener("accountsChanged", onAccounts);
      current.provider.removeListener("chainChanged", onChain);
    }
    current = w;
    try { localStorage.setItem(key, w.id); } catch (_) { /* privacy mode */ }
    if (w.provider.on) { w.provider.on("accountsChanged", onAccounts); w.provider.on("chainChanged", onChain); }
    return w.provider;
  }
  function onAccounts(accounts) { accountListeners.forEach(function (fn) { fn(accounts); }); }
  function onChain(chain) { chainListeners.forEach(function (fn) { fn(chain); }); }
  function dialog(title, body, onDismiss) {
    var host = document.getElementById("dialog-host");
    host.innerHTML = '<div class="modal"><section class="modal-box" role="dialog" aria-modal="true" aria-label="' + esc(title) + '"><div class="card-head"><h2>' + esc(title) + '</h2><button class="close" data-close>关闭</button></div>' + body + '</section></div>';
    var overlay = host.firstElementChild;
    function close(dismissed) { host.innerHTML = ""; document.removeEventListener("keydown", escape); if (dismissed && onDismiss) onDismiss(); }
    function escape(e) { if (e.key === "Escape") close(true); }
    document.addEventListener("keydown", escape);
    overlay.addEventListener("click", function (e) { if (e.target === overlay || e.target.closest("[data-close]")) close(true); });
    return { el: overlay, close: close };
  }
  function noWallet() {
    var url = location.href, mobile = /Android|iPhone|iPad/i.test(navigator.userAgent);
    var urls = mobile ? [["OKX Wallet", "okx://wallet/dapp/url?dappUrl=" + encodeURIComponent(url)], ["Bitget Wallet", "bitkeep://bkconnect?action=dapp&url=" + encodeURIComponent(url)], ["TokenPocket", "tpdapp://open?params=" + encodeURIComponent(JSON.stringify({ url: url }))], ["MetaMask", "https://metamask.app.link/dapp/" + url.replace(/^https?:\/\//, "")]] : [["MetaMask", "https://metamask.io/download/"], ["OKX Wallet", "https://www.okx.com/web3"], ["Bitget Wallet", "https://web3.bitget.com/"]];
    dialog(mobile ? "在钱包 App 里打开" : "没有检测到钱包", '<p>' + (mobile ? "在钱包 App 的 DApp 浏览器打开当前网址，然后连接钱包。" : "安装浏览器钱包后刷新本页，或在手机钱包的 DApp 浏览器里打开本站。") + '</p><div>' + urls.map(function (x) { return '<a class="wallet-choice" target="_blank" rel="noopener" href="' + esc(x[1]) + '">' + esc(x[0]) + ' ↗</a>'; }).join("") + '</div><p class="mono">' + esc(url) + '</p>');
  }
  async function choose() {
    if (current) return current.provider;
    await new Promise(function (resolve) { setTimeout(resolve, 150); });
    var wallets = list(), previous = wallets.find(function (w) { return w.id === saved(); });
    if (previous) return use(previous);
    if (wallets.length === 1) return use(wallets[0]);
    if (!wallets.length) { noWallet(); throw new Error("请在钱包浏览器中打开，或先安装钱包插件"); }
    return new Promise(function (resolve, reject) {
      var d = dialog("选择钱包", '<p>选择用于 FLGNS OTC 的钱包。</p>' + wallets.map(function (w, i) {
        var icon = w.icon && /^data:image\/(png|jpeg|webp|svg\+xml);/i.test(w.icon) ? '<img alt="" src="' + esc(w.icon) + '">' : '<span class="wallet-letter">' + esc(w.name.slice(0, 1)) + '</span>';
        return '<button class="wallet-choice" data-wallet="' + i + '">' + icon + esc(w.name) + '</button>';
      }).join(""), function () { reject(new Error("已取消选择钱包")); });
      d.el.addEventListener("click", function (e) {
        var b = e.target.closest("[data-wallet]");
        if (b) { d.close(); resolve(use(wallets[Number(b.dataset.wallet)])); }
        else if (e.target === d.el || e.target.closest("[data-close]")) reject(new Error("已取消选择钱包"));
      });
    });
  }
  async function switchChain(provider) {
    var c = window.FLGNS_CONFIG.chain, hex = "0x" + c.id.toString(16);
    if (BigInt(await provider.request({ method: "eth_chainId" })) === BigInt(c.id)) return;
    try { await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: hex }] }); }
    catch (e) {
      if (e.code !== 4902 && !(e.data && e.data.originalError && e.data.originalError.code === 4902)) throw e;
      await provider.request({ method: "wallet_addEthereumChain", params: [{ chainId: hex, chainName: c.name, nativeCurrency: c.nativeCurrency, rpcUrls: c.rpcUrls, blockExplorerUrls: [c.explorer] }] });
      await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: hex }] });
    }
    if (BigInt(await provider.request({ method: "eth_chainId" })) !== BigInt(c.id)) throw new Error("请将钱包切换到 Anubis 网络");
  }
  async function connect() {
    var p = await choose(), accounts = await p.request({ method: "eth_requestAccounts" });
    if (!accounts || !/^0x[0-9a-f]{40}$/i.test(accounts[0])) throw new Error("钱包未返回可用地址");
    await switchChain(p);
    return accounts[0].toLowerCase();
  }
  async function silent() {
    await new Promise(function (resolve) { setTimeout(resolve, 150); });
    var wallets = list(), w = wallets.find(function (x) { return x.id === saved(); }) || (wallets.length === 1 ? wallets[0] : null);
    if (!w) return null;
    try { var accounts = await w.provider.request({ method: "eth_accounts" }); if (!accounts || !accounts[0]) return null; use(w); return accounts[0].toLowerCase(); } catch (_) { return null; }
  }
  function error(e) {
    if (e && (e.code === 4001 || e.code === "ACTION_REJECTED")) return "你已取消钱包操作";
    if (e && e.code === -32002) return "钱包已有等待确认的请求，请打开钱包处理";
    var m = String(e && (e.shortMessage || e.message) || "操作失败，请重试");
    var reasons = { TRANSFER_MISMATCH: "代币实际到账数量不符，可能存在转账税或转账限制，交易已回退", TOKEN_TRANSFER_FAILED: "代币转账失败，请检查余额、授权和代币限制", NO_ORDER: "订单不存在", CLOSED: "订单已结清，请刷新", EXPIRED: "订单已到期，请刷新", BAD_AMOUNT: "成交数量超出订单余额，请刷新", PRICE_CHANGED: "应付金额已变化，请重新核对", NOT_MAKER: "只有挂单方可以撤单", BAD_DURATION: "有效期须在10分钟到30天之间", SELF_TRADE: "不能成交自己的挂单，请使用撤单", ZERO_AMOUNT: "数量必须大于0", REENTRANT: "合约拒绝重复调用" };
    for (var k in reasons) if (m.includes(k)) return reasons[k];
    return m.length > 240 ? m.slice(0, 240) + "…" : m;
  }
  window.FLGNSWallet = { connect: connect, silent: silent, get: function () { return current && current.provider; }, switchChain: switchChain, name: function () { return current && current.name; }, onAccounts: function (fn) { accountListeners.push(fn); }, onChain: function (fn) { chainListeners.push(fn); }, dialog: dialog, error: error, esc: esc };
})();
