(function () {
  "use strict";
  var C = window.FLGNS_CONFIG, N = window.FLGNSAmounts, W = window.FLGNSWallet, esc = W.esc;
  var keys = ["dai", "lgns", "ailgns", "rm"], marketKey = keys.includes(location.hash.slice(1)) ? location.hash.slice(1) : "dai";
  var account = null, side = "sell", orders = [], verified = false, loading = false, busy = false, generation = 0, pages = { buys: 0, sells: 0, mine: 0 }, pageSize = 20, blockTag = "latest";
  var SEL = { create: "0x9128a740", fill: "0x6fe4a448", cancel: "0x40e58ee5", getOrder: "0xd09ef241", getOrders: "0x8f72fc77", ordersCount: "0x35daa731", refund: "0x34bfca5c", approve: "0x095ea7b3", allowance: "0xdd62ed3e", balance: "0x70a08231", decimals: "0x313ce567", tokenA: "0x93080cd0", tokenB: "0x499f712e", feeBps: "0xbf333f2c", recipient60: "0x50abfee1", recipient40: "0x48b4ebda", share60: "0x3363c2a7" };
  function $(id) { return document.getElementById(id); }
  function market() { return C.markets[marketKey]; }
  function short(s) { return s.slice(0, 6) + "…" + s.slice(-4); }
  function word(v) { var h = BigInt(v).toString(16); if (h.length > 64 || h.startsWith("-")) throw new Error("数值超出合约范围"); return h.padStart(64, "0"); }
  function addr(a) { if (!/^0x[0-9a-f]{40}$/i.test(a)) throw new Error("无效合约地址"); return a.toLowerCase(); }
  function addressLink(a, label) { return '<a class="mono" target="_blank" rel="noopener" href="' + C.chain.explorer + '/address/' + addr(a) + '">' + esc(label || a) + ' ↗</a>'; }
  function tokenDisplay(v, token) { return N.format(v, token.decimals, true); }
  function toast(message) { $("toast").textContent = message; $("toast").hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(function () { $("toast").hidden = true; }, 6500); }
  function setStatus(id, text, error) { var el = $(id); if (!el) return; el.textContent = text; el.classList.toggle("error", !!error); }
  function banner(text, type) { $("deployment-status").textContent = text; $("deployment-status").className = "status-banner" + (type ? " " + type : ""); }
  function configured() { var m = market(); return C.deploymentReady && /^0x[0-9a-f]{40}$/i.test(m.escrow) && Number.isInteger(C.tokenA.decimals) && Number.isInteger(m.decimals); }
  function controls() {
    $("create-submit").disabled = !verified || busy;
    $("create-submit").textContent = busy ? "正在处理钱包交易…" : verified ? (account ? "授权并发布" + (side === "sell" ? "卖单" : "买单") : "连接钱包并发布挂单") : "市场尚未开放";
    $("refresh-book").disabled = loading || busy || !configured();
    document.querySelectorAll("[data-market], [data-side], [data-goform]").forEach(function (b) { b.disabled = busy; });
    $("refund-expired").disabled = busy || !verified || !account || !orders.some(function (o) { return o.open && o.giveLeft > 0n && o.maker === account && o.expiry <= Date.now() / 1000; });
    $("lookup-order").disabled = !verified || busy;
    $("connect-wallet").disabled = busy;
  }
  async function rpc(method, params) {
    var last;
    for (var i = 0; i < C.chain.rpcUrls.length; i++) {
      var abort = new AbortController(), timer = setTimeout(function () { abort.abort(); }, 12000);
      try {
        var response = await fetch(C.chain.rpcUrls[i], { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: Date.now(), method: method, params: params || [] }), signal: abort.signal });
        if (!response.ok) throw new Error("节点返回 HTTP " + response.status);
        var result = await response.json();
        if (result.error) {
          var message = result.error.message || "链上请求失败", data = result.error.data;
          if (typeof data === "string" && data.startsWith("0x08c379a0")) {
            try { var length = Number(BigInt("0x" + data.slice(74, 138))); message += ": " + new TextDecoder().decode(new Uint8Array(data.slice(138, 138 + length * 2).match(/../g).map(function (x) { return parseInt(x, 16); }))); } catch (_) { /* preserve RPC message */ }
          }
          var err = new Error(message); err.code = result.error.code; throw err;
        }
        if (result.result === undefined) throw new Error("节点没有返回数据");
        return result.result;
      } catch (e) { last = e; }
      finally { clearTimeout(timer); }
    }
    throw last || new Error("链上节点暂时不可用");
  }
  function call(to, data, block) { return rpc("eth_call", [{ to: addr(to), data: data }, block || "latest"]); }
  function readInteger(h) { if (!/^0x[0-9a-f]+$/i.test(h)) throw new Error("链上返回的数值无效"); return BigInt(h); }
  function readAddress(h) { if (!/^0x[0-9a-f]{64}$/i.test(h)) throw new Error("链上返回的地址无效"); return addr("0x" + h.slice(-40)); }
  function decodeOrder(w, id) {
    if (w.length !== 7 || w.some(function (s) { return !/^[0-9a-f]{64}$/i.test(s); })) throw new Error("订单数据结构不匹配");
    var flag = BigInt("0x" + w[1]), open = BigInt("0x" + w[2]), expiry = BigInt("0x" + w[3]);
    if (flag > 1n || open > 1n || expiry > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("订单数据无效");
    return { id: id, maker: "0x" + w[0].slice(24).toLowerCase(), sellFlgns: flag === 1n, open: open === 1n, expiry: Number(expiry), giveTotal: BigInt("0x" + w[4]), wantTotal: BigInt("0x" + w[5]), giveLeft: BigInt("0x" + w[6]) };
  }
  function words(h) { if (!/^0x(?:[0-9a-f]{64})*$/i.test(h)) throw new Error("链上返回格式错误"); return h.slice(2).match(/.{64}/g) || []; }
  function decodeOrders(h, from, expected) {
    var w = words(h);
    if (w.length < 2 || BigInt("0x" + w[0]) !== 32n || BigInt("0x" + w[1]) !== BigInt(expected) || w.length !== 2 + expected * 7) throw new Error("订单分页数据不完整，请重新读取");
    var out = []; for (var i = 0; i < expected; i++) out.push(decodeOrder(w.slice(2 + i * 7, 9 + i * 7), from + i)); return out;
  }
  async function verifyMarket(m) {
    var result = await Promise.all([
      rpc("eth_chainId", []), rpc("eth_getCode", [m.escrow, "latest"]),
      call(C.tokenA.address, SEL.decimals), call(m.address, SEL.decimals),
      call(m.escrow, SEL.tokenA), call(m.escrow, SEL.tokenB), call(m.escrow, SEL.feeBps),
      call(m.escrow, SEL.recipient60), call(m.escrow, SEL.recipient40), call(m.escrow, SEL.share60)
    ]);
    if (readInteger(result[0]) !== BigInt(C.chain.id) || result[1] === "0x" || result[1] === "0x0") throw new Error("合约或链ID核对失败");
    if (readInteger(result[2]) !== BigInt(C.tokenA.decimals) || readInteger(result[3]) !== BigInt(m.decimals)) throw new Error("代币精度与配置不一致，已暂停交易");
    if (readAddress(result[4]) !== addr(C.tokenA.address) || readAddress(result[5]) !== addr(m.address) || C.feeBps !== 40 || readInteger(result[6]) !== 40n || readAddress(result[7]) !== addr(C.feeRecipient60) || readAddress(result[8]) !== addr(C.feeRecipient40) || readInteger(result[9]) !== 6000n) throw new Error("合约代币或手续费配置与预期不一致，已暂停交易");
  }
  function renderTabs() {
    $("market-tabs").innerHTML = keys.map(function (k) { var m = C.markets[k]; return '<button class="market-tab' + (k === marketKey ? " on" : "") + '" data-market="' + k + '" aria-current="' + (k === marketKey ? "page" : "false") + '">FLGNS <span class="slash">/</span> ' + esc(m.symbol) + '</button>'; }).join("");
    $("current-market").textContent = "FLGNS / " + market().symbol;
    document.querySelectorAll("[data-legacy-link]").forEach(function (el) { el.href = el.dataset.section ? "legacy/?market=" + marketKey + "#" + el.dataset.section : "legacy/#" + marketKey; });
    document.querySelectorAll(".quote-symbol").forEach(function (el) { el.textContent = market().symbol; });
  }
  function renderContract() {
    var m = market();
    $("contract-details").innerHTML = '<div class="contract-item"><span>当前交易对托管合约 · FLGNS / ' + esc(m.symbol) + '</span>' + (/^0x[0-9a-f]{40}$/i.test(m.escrow) ? addressLink(m.escrow) : '<b class="muted">尚未部署 · 交易功能未开放</b>') + '</div><div class="contract-item"><span>网络</span><b>Anubis · Chain ID 6714</b>' + (C.sourceUrl ? ' · <a target="_blank" rel="noopener" href="' + esc(C.sourceUrl) + '">开源合约 ↗</a>' : "") + (C.deploymentUrl ? ' · <a target="_blank" rel="noopener" href="' + esc(C.deploymentUrl) + '">部署核对 ↗</a>' : "") + '</div><div class="contract-item"><span>FLGNS 代币' + (Number.isInteger(C.tokenA.decimals) ? " · " + C.tokenA.decimals + " 位精度" : " · 精度待验证") + '</span>' + addressLink(C.tokenA.address) + '</div><div class="contract-item"><span>' + esc(m.symbol) + ' 代币' + (Number.isInteger(m.decimals) ? " · " + m.decimals + " 位精度" : " · 精度待验证") + '</span>' + addressLink(m.address) + '</div>';
    $("fee-60").href = C.chain.explorer + "/address/" + C.feeRecipient60; $("fee-60").textContent = C.feeRecipient60;
    $("fee-40").href = C.chain.explorer + "/address/" + C.feeRecipient40; $("fee-40").textContent = C.feeRecipient40;
  }
  function emptyBook(message) { return '<div class="empty"><svg viewBox="0 0 32 32" aria-hidden="true"><path d="M8 5h16v23H8zM12 11h8M12 16h8M12 21h5"/><path d="M11 5V3h10v2"/></svg><b>' + esc(message) + '</b><small>' + (verified ? "你可以发布挂单，等待对方成交" : "市场开放后，将显示真实链上订单") + '</small></div>'; }
  function timeLeft(o) { var s = o.expiry - Math.floor(Date.now() / 1000); if (!o.open || !o.giveLeft) return "已结清"; if (s <= 0) return "到期 · 待取回"; var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), min = Math.floor(s % 3600 / 60); return d ? d + "天 " + h + "小时" : h ? h + "小时 " + min + "分" : Math.max(1, min) + "分钟"; }
  function price(o) { var q = o.sellFlgns ? o.wantTotal : o.giveTotal, a = o.sellFlgns ? o.giveTotal : o.wantTotal; return N.ratio(q, a, market().decimals, C.tokenA.decimals, 12); }
  function remainingA(o) { return o.sellFlgns ? o.giveLeft : N.ceilDiv(o.giveLeft * o.wantTotal, o.giveTotal); }
  function button(o) {
    if (!o.open || !o.giveLeft) return '<span class="muted">已结清</span>';
    if (o.maker === account) return '<button class="order-action cancel" data-cancel="' + o.id + '"' + (busy ? " disabled" : "") + '>' + (o.expiry <= Date.now() / 1000 ? "取回" : "撤单") + '</button>';
    if (o.expiry <= Date.now() / 1000) return '<button class="order-action cancel" data-refund="' + o.id + '">触发退款</button>';
    return '<button class="order-action' + (o.sellFlgns ? "" : " sell") + '" data-fill="' + o.id + '"' + (busy ? " disabled" : "") + '>' + (o.sellFlgns ? "买入" : "卖出") + '</button>';
  }
  function pager(key, total) { var count = Math.ceil(total / pageSize); if (count <= 1) return ""; return '<div class="pager"><button data-page="' + key + '" data-step="-1"' + (!pages[key] ? " disabled" : "") + '>上一页</button><span>第 ' + (pages[key] + 1) + ' / ' + count + ' 页 · 共 ' + total + ' 单</span><button data-page="' + key + '" data-step="1"' + (pages[key] + 1 >= count ? " disabled" : "") + '>下一页</button></div>'; }
  function slicePage(key, list) { pages[key] = Math.min(pages[key], Math.max(0, Math.ceil(list.length / pageSize) - 1)); return list.slice(pages[key] * pageSize, (pages[key] + 1) * pageSize); }
  function renderBooks() {
    var live = verified ? orders.filter(function (o) { return o.open && o.giveLeft > 0n && o.expiry > Date.now() / 1000; }) : [];
    function compare(a, b) { var left = (a.sellFlgns ? a.wantTotal : a.giveTotal) * (b.sellFlgns ? b.giveTotal : b.wantTotal), right = (b.sellFlgns ? b.wantTotal : b.giveTotal) * (a.sellFlgns ? a.giveTotal : a.wantTotal); return left < right ? -1 : left > right ? 1 : a.id - b.id; }
    var buys = live.filter(function (o) { return !o.sellFlgns; }).sort(function (a, b) { return -compare(a, b); });
    var sells = live.filter(function (o) { return o.sellFlgns; }).sort(compare);
    function book(key, list, title, hint, isSell) {
      var selected = slicePage(key, list);
      return '<section class="card"><div class="card-head"><h2 class="book-title"><span class="book-indicator' + (isSell ? " sell" : "") + '">' + (isSell ? "↗" : "↙") + '</span>' + title + '<span class="book-count">' + (verified ? list.length : "—") + ' 单</span></h2><span class="book-hint">' + hint + '</span></div>' + (selected.length ? '<div class="table-wrap"><table><thead><tr><th>操作</th><th>扣费前单价 ' + esc(market().symbol) + '</th><th>剩余 FLGNS</th><th>托管剩余</th><th>剩余时间</th><th>挂单人</th></tr></thead><tbody>' + selected.map(function (o) { return '<tr><td>' + button(o) + '</td><td class="price' + (isSell ? " sell-price" : "") + '">' + esc(price(o)) + '</td><td>' + esc(tokenDisplay(remainingA(o), C.tokenA)) + '</td><td>' + esc(tokenDisplay(o.giveLeft, o.sellFlgns ? C.tokenA : market())) + ' ' + esc(o.sellFlgns ? "FLGNS" : market().symbol) + '</td><td>' + esc(timeLeft(o)) + '</td><td class="mono">' + (o.maker === account ? "我的订单" : esc(short(o.maker))) + '</td></tr>'; }).join("") + '</tbody></table></div>' + pager(key, list.length) : emptyBook(loading ? "正在读取全部链上订单…" : verified ? "暂无挂单，发布第一笔交易" : "交易对即将开放")) + '</section>';
    }
    $("order-books").innerHTML = book("buys", buys, "卖出 FLGNS", "成交买单 · 价格从高到低", true) + book("sells", sells, "买入 FLGNS", "成交卖单 · 价格从低到高", false);
    renderMine();
  }
  function renderMine() {
    if (!account) { $("my-order-list").className = "empty"; $("my-order-list").textContent = "连接钱包后显示当前交易对的全部未结清订单"; return; }
    if (!verified) { $("my-order-list").className = "empty"; $("my-order-list").textContent = "市场尚未开放，暂时无法读取订单"; return; }
    var mine = orders.filter(function (o) { return o.maker === account && ($("mine-filter").value === "all" || o.open && o.giveLeft > 0n); }).sort(function (a, b) { return b.id - a.id; });
    $("my-order-list").className = "";
    $("my-order-list").innerHTML = mine.length ? '<div class="table-wrap"><table class="my-table"><thead><tr><th>订单编号</th><th>方向</th><th>托管剩余</th><th>状态 / 到期时间</th><th>操作</th></tr></thead><tbody>' + slicePage("mine", mine).map(function (o) { return '<tr><td>#' + o.id + '</td><td>' + (o.sellFlgns ? "卖出" : "买入") + ' FLGNS</td><td>' + esc(tokenDisplay(o.giveLeft, o.sellFlgns ? C.tokenA : market())) + ' ' + esc(o.sellFlgns ? "FLGNS" : market().symbol) + '</td><td>' + esc(timeLeft(o)) + '</td><td>' + button(o) + '</td></tr>'; }).join("") + '</tbody></table></div>' + pager("mine", mine.length) + '<p class="note">' + ($("mine-filter").value === "open" ? "当前交易对全部未结清订单，包含已到期未退款订单。" : "当前交易对该钱包的完整订单历史。") + '</p>' : '<div class="empty">当前交易对没有' + ($("mine-filter").value === "open" ? "未结清" : "") + '订单</div>';
    controls();
  }
  async function refresh() {
    var id = ++generation, m = market(); verified = false; loading = false; orders = []; pages = { buys: 0, sells: 0, mine: 0 }; renderTabs(); renderContract(); renderBooks(); controls();
    $("lookup-result").innerHTML = "";
    if (!configured()) { banner("托管合约尚未完成部署验证，当前为界面预览，挂单与成交暂未开放。", "pending"); $("book-status").textContent = "尚未开放 · 无模拟订单"; return; }
    loading = true; banner("正在验证代币、托管合约及手续费配置…"); controls();
    try {
      await verifyMarket(m); if (id !== generation) return;
      blockTag = await rpc("eth_blockNumber", []);
      var countBig = readInteger(await call(m.escrow, SEL.ordersCount, blockTag));
      if (countBig > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("订单总量超过浏览器可索引范围，请使用订单编号查询");
      var count = Number(countBig), results = [], chunkSize = 100;
      for (var from = 0; from < count; from += chunkSize * 3) {
        var jobs = [];
        for (var start = from; start < Math.min(count, from + chunkSize * 3); start += chunkSize) (function (offset) { var amount = Math.min(chunkSize, count - offset); jobs.push(call(m.escrow, SEL.getOrders + word(offset) + word(amount), blockTag).then(function (h) { return decodeOrders(h, offset, amount); })); })(start);
        var group = await Promise.all(jobs); if (id !== generation) return;
        group.forEach(function (list) { results.push.apply(results, list); });
        $("book-status").textContent = "读取全部订单 " + results.length + " / " + count;
      }
      if (id !== generation) return;
      orders = results; verified = true; loading = false;
      banner("合约配置已验证 · 挂单方、吃单方各从自己收到的币中扣除 0.4%");
      $("book-status").textContent = "已读取全部 " + count + " 单 · " + new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
      renderBooks(); controls(); await updateWallet();
    } catch (e) {
      if (id !== generation) return;
      verified = false; loading = false; banner(W.error(e) + "。链上数据未完成核对，交易暂不可用。", "error"); $("book-status").textContent = "读取失败 · 请刷新重试"; renderBooks(); controls();
    }
  }
  function updateSide(value) {
    side = value; document.querySelectorAll("[data-side]").forEach(function (b) { b.classList.toggle("on", b.dataset.side === side); });
    $("quantity-label").textContent = side === "sell" ? "卖出 FLGNS 数量（扣费前）" : "买入 FLGNS 数量（扣费前）";
    $("quote-label").textContent = side === "sell" ? "成交报价总额（扣费前）" : "托管支付总额（扣费前）";
    $("create-note").textContent = side === "sell" ? "挂单时锁定扣费前 FLGNS；成交后从收到的 " + market().symbol + " 中扣除 0.4%。未成交部分可随时撤单取回。" : "挂单时锁定扣费前 " + market().symbol + "；成交后从收到的 FLGNS 中扣除 0.4%。未成交部分可随时撤单取回。";
    controls(); previewPrice();
  }
  function previewPrice() {
    if (!$("flgns-amount").value || !$("quote-amount").value) { $("price-preview").textContent = "填写扣费前数量和报价，预览双方手续费与净收"; return; }
    try {
      var a = N.parse($("flgns-amount").value, C.tokenA.decimals), b = N.parse($("quote-amount").value, market().decimals);
      if (!a || !b) throw new Error("数量和总额须大于 0");
      var sell = side === "sell", giveToken = sell ? C.tokenA : market(), payToken = sell ? market() : C.tokenA;
      var values = N.settlement(sell ? a : b, sell ? a : b, sell ? b : a, C.feeBps);
      $("price-preview").innerHTML = '扣费前单价：<strong>' + esc(N.ratio(b, a, market().decimals, C.tokenA.decimals, 12)) + '</strong> ' + esc(market().symbol) + ' / FLGNS' +
        '<div class="fee-preview"><span>你托管：' + esc(tokenDisplay(values.grossGive, giveToken)) + ' ' + esc(giveToken.symbol) + '（扣费前）</span><span>你净收：<strong>' + esc(tokenDisplay(values.makerNet, payToken)) + ' ' + esc(payToken.symbol) + '</strong> · 手续费 ' + esc(tokenDisplay(values.makerFee, payToken)) + ' ' + esc(payToken.symbol) + '（0.4%）</span><span>吃单方支付：' + esc(tokenDisplay(values.grossPay, payToken)) + ' ' + esc(payToken.symbol) + ' · 净收 ' + esc(tokenDisplay(values.takerNet, giveToken)) + ' ' + esc(giveToken.symbol) + ' · 手续费 ' + esc(tokenDisplay(values.takerFee, giveToken)) + ' ' + esc(giveToken.symbol) + '（0.4%）</span><span>整单一次成交预估；部分成交按原始总额比例计算，付款向上取整，手续费逐笔向下取整。</span></div>';
    } catch (e) { $("price-preview").textContent = W.error(e); }
  }
  async function connect() {
    try { account = await W.connect(); $("connect-wallet").textContent = short(account); await updateWallet(); renderMine(); renderBooks(); controls(); return account; }
    catch (e) { toast(W.error(e)); throw e; }
  }
  async function updateWallet() {
    if (!account) return;
    var who = account, mk = marketKey, m = market();
    $("connect-wallet").textContent = short(who);
    $("wallet-details").innerHTML = '<div class="wallet-address"><div class="muted" style="font-size:10px;margin-bottom:4px">' + esc(W.name() || "已连接钱包") + '</div>' + addressLink(who) + '</div><div id="wallet-balances" class="muted" style="font-size:12px;padding:15px 0">正在读取余额…</div>';
    try {
      var result = await Promise.all([call(C.tokenA.address, SEL.balance + word(who)), call(m.address, SEL.balance + word(who)), rpc("eth_getBalance", [who, "latest"])]);
      if (account !== who || marketKey !== mk || !$("wallet-balances")) return;
      $("wallet-balances").innerHTML = '<div class="balance-row"><span>FLGNS</span><b>' + (Number.isInteger(C.tokenA.decimals) ? esc(tokenDisplay(readInteger(result[0]), C.tokenA)) : "精度待验证") + '</b></div><div class="balance-row"><span>' + esc(m.symbol) + '</span><b>' + (Number.isInteger(m.decimals) ? esc(tokenDisplay(readInteger(result[1]), m)) : "精度待验证") + '</b></div><div class="balance-row"><span>Gas 余额</span><b>' + esc(N.format(readInteger(result[2]), C.chain.nativeCurrency.decimals, true)) + ' <small>原生 DAI</small></b></div>';
    } catch (e) { if (account === who && marketKey === mk && $("wallet-balances")) $("wallet-balances").textContent = "余额读取失败：" + W.error(e); }
  }
  function context() { if (!verified || !configured()) throw new Error("市场尚未完成验证，请刷新后重试"); return { key: marketKey, m: market(), who: account }; }
  async function assertWallet(ctx) {
    if (ctx.key !== marketKey || !verified) throw new Error("交易对已变化，请重新核对");
    var provider = W.get(); if (!provider) throw new Error("请先连接钱包");
    await W.switchChain(provider);
    var accounts = await provider.request({ method: "eth_accounts" });
    if (!accounts || !accounts[0] || accounts[0].toLowerCase() !== ctx.who) throw new Error("钱包地址已变化，请重新连接后操作");
    return provider;
  }
  async function receipt(hash, say) {
    for (var i = 0; i < 180; i++) {
      var r = await rpc("eth_getTransactionReceipt", [hash]);
      if (r) { if (readInteger(r.status) !== 1n) throw new Error("链上交易已回退，操作未成功；请在区块浏览器核对"); return r; }
      await new Promise(function (resolve) { setTimeout(resolve, 2000); });
      if (i % 15 === 0) say("交易已提交，正在等待确认：" + short(hash));
    }
    throw new Error("交易仍待确认，请在区块浏览器核对交易 " + hash + "，不要重复提交");
  }
  async function send(ctx, to, data, say, label) {
    var provider = await assertWallet(ctx), tx = { from: ctx.who, to: addr(to), data: data };
    say("正在模拟：" + label + "…");
    await rpc("eth_call", [tx, "latest"]);
    var estimate = readInteger(await rpc("eth_estimateGas", [tx])); tx.gas = "0x" + (estimate * 13n / 10n).toString(16);
    await assertWallet(ctx); say("请在钱包中确认：" + label);
    var hash = await provider.request({ method: "eth_sendTransaction", params: [tx] });
    if (!/^0x[0-9a-f]{64}$/i.test(hash)) throw new Error("钱包未返回有效交易哈希，请先核对钱包交易记录");
    say(label + "已提交，等待链上确认：" + hash);
    var confirmed;
    try { confirmed = await receipt(hash, say); }
    catch (e) { throw new Error(label + "已提交（" + hash + "）。" + W.error(e) + "。请先在区块浏览器核对这笔交易，不要重复提交。"); }
    say(label + "已链上确认：" + hash); return confirmed;
  }
  async function ensureAllowance(ctx, token, amount, say) {
    var result = await Promise.all([call(token.address, SEL.balance + word(ctx.who)), call(token.address, SEL.allowance + word(ctx.who) + word(ctx.m.escrow))]);
    if (readInteger(result[0]) < amount) throw new Error(token.symbol + " 余额不足");
    var allowed = readInteger(result[1]); if (allowed >= amount) return;
    if (allowed > 0n) await send(ctx, token.address, SEL.approve + word(ctx.m.escrow) + word(0), say, "重置原有 " + token.symbol + " 授权");
    await send(ctx, token.address, SEL.approve + word(ctx.m.escrow) + word(amount), say, "授权本次需要的 " + N.format(amount, token.decimals, true) + " " + token.symbol);
  }
  async function operation(statusId, fn) {
    if (busy) return;
    var initialKey = marketKey;
    busy = true; controls();
    function say(s) { setStatus(statusId, s, false); }
    say("正在核对钱包和交易对…");
    try {
      if (!account) await connect();
      if (marketKey !== initialKey) throw new Error("交易对已变化，请重新核对");
      var ctx = context();
      await fn(ctx, say); await refresh();
    }
    catch (e) { setStatus(statusId, W.error(e), true); toast(W.error(e)); }
    finally { busy = false; controls(); renderBooks(); }
  }
  async function createOrder(event) {
    event.preventDefault();
    var a, b, sell = side === "sell", duration = Number($("duration").value);
    try { a = N.parse($("flgns-amount").value, C.tokenA.decimals); b = N.parse($("quote-amount").value, market().decimals); if (a <= 0n || b <= 0n) throw new Error("数量和总额须大于 0"); if (!Number.isInteger(duration) || duration < 600 || duration > 2592000) throw new Error("有效期须在10分钟到30天之间"); }
    catch (e) { setStatus("create-status", W.error(e), true); return; }
    if (!verified || busy) { setStatus("create-status", "市场尚未完成验证，请刷新后重试", true); return; }
    var mk = marketKey, give = sell ? a : b, want = sell ? b : a, giveToken = sell ? C.tokenA : market(), payToken = sell ? market() : C.tokenA;
    var values = N.settlement(give, give, want, C.feeBps);
    var dialog = W.dialog("确认发布" + (sell ? "卖出" : "买入") + " FLGNS 挂单", '<div class="confirm-details"><div><span>你托管（扣费前）</span><b>' + esc(tokenDisplay(give, giveToken)) + ' ' + esc(giveToken.symbol) + '</b></div><div><span>成交报价（扣费前）</span><b>' + esc(tokenDisplay(want, payToken)) + ' ' + esc(payToken.symbol) + '</b></div><div><span>你的手续费 · 0.4%</span><b>' + esc(tokenDisplay(values.makerFee, payToken)) + ' ' + esc(payToken.symbol) + '</b></div><div><span>你实际净收</span><b>' + esc(tokenDisplay(values.makerNet, payToken)) + ' ' + esc(payToken.symbol) + '</b></div><div><span>吃单方手续费 · 0.4%</span><b>' + esc(tokenDisplay(values.takerFee, giveToken)) + ' ' + esc(giveToken.symbol) + '</b></div><div><span>吃单方实际净收</span><b>' + esc(tokenDisplay(values.takerNet, giveToken)) + ' ' + esc(giveToken.symbol) + '</b></div></div><p>以上为整单一次成交预估；部分成交按比例计算，付款向上取整，两边手续费逐笔向下取整。未成交部分可撤单原额取回。</p><button class="primary wide" id="confirm-create">授权并发布挂单</button><p class="operation-status" id="confirm-create-status" role="status"></p>');
    $("confirm-create").onclick = async function () {
      $("confirm-create").disabled = true;
      await operation("confirm-create-status", async function (ctx, say) {
        if (ctx.key !== mk) throw new Error("交易对已变化，请重新确认挂单");
        await ensureAllowance(ctx, giveToken, give, say);
        await send(ctx, ctx.m.escrow, SEL.create + word(sell ? 1 : 0) + word(give) + word(want) + word(duration), say, "发布 " + (sell ? "卖出" : "买入") + " FLGNS 挂单");
        dialog.close(); $("flgns-amount").value = ""; $("quote-amount").value = ""; previewPrice();
      });
      if ($("confirm-create")) $("confirm-create").disabled = false;
    };
  }
  async function lookupOrder(id) {
    var m = market(), h = await call(m.escrow, SEL.getOrder + word(id)); return decodeOrder(words(h), id);
  }
  async function fillDialog(id) {
    if (busy || !verified) return;
    var mk = marketKey, o;
    try { o = await lookupOrder(id); } catch (e) { toast(W.error(e)); return; }
    if (mk !== marketKey) return;
    if (!o.open || !o.giveLeft || o.expiry <= Date.now() / 1000) { toast("订单已结清或到期，请刷新"); return; }
    if (o.maker === account) { toast("这是你的挂单，请使用撤单"); return; }
    var giveToken = o.sellFlgns ? C.tokenA : market(), payToken = o.sellFlgns ? market() : C.tokenA;
    var dialog = W.dialog(o.sellFlgns ? "买入 FLGNS" : "卖出 FLGNS", '<p>成交订单 #' + id + ' · 挂单方 ' + esc(short(o.maker)) + '</p><label class="field"><span>领取 ' + esc(giveToken.symbol) + ' 的数量（扣费前）</span><div class="input-wrap"><input id="fill-amount" inputmode="decimal" autocomplete="off" value="' + N.format(o.giveLeft, giveToken.decimals) + '"><b>' + esc(giveToken.symbol) + '</b></div></label><div id="fill-preview" class="confirm-details"></div><p>双方各从自己收到的币中扣除 0.4%；你的付款总额不会另加手续费。付款金额按挂单比例向上取整，手续费按各代币精度向下取整。请核对钱包中的合约和金额。</p><button class="primary wide" id="confirm-fill">授权并确认成交</button><p class="operation-status" id="fill-status" role="status"></p>');
    function estimate() {
      var amount = N.parse($("fill-amount").value, giveToken.decimals); if (!amount || amount > o.giveLeft) throw new Error("数量必须大于0且不超过订单剩余数量");
      var values = N.settlement(amount, o.giveTotal, o.wantTotal, C.feeBps);
      $("fill-preview").innerHTML = '<div><span>你需支付（扣费前总额）</span><b>' + esc(tokenDisplay(values.grossPay, payToken)) + ' ' + esc(payToken.symbol) + '</b></div><div><span>你的手续费 · 0.4%</span><b>' + esc(tokenDisplay(values.takerFee, giveToken)) + ' ' + esc(giveToken.symbol) + '</b></div><div><span>你实际净收</span><b>' + esc(tokenDisplay(values.takerNet, giveToken)) + ' ' + esc(giveToken.symbol) + '</b></div><div><span>挂单方手续费 · 0.4%</span><b>' + esc(tokenDisplay(values.makerFee, payToken)) + ' ' + esc(payToken.symbol) + '</b></div><div><span>挂单方实际净收</span><b>' + esc(tokenDisplay(values.makerNet, payToken)) + ' ' + esc(payToken.symbol) + '</b></div>';
      return { amount: amount, pay: values.grossPay };
    }
    function showEstimate() { try { estimate(); $("confirm-fill").disabled = false; } catch (e) { $("fill-preview").textContent = W.error(e); $("confirm-fill").disabled = true; } }
    showEstimate(); $("fill-amount").addEventListener("input", showEstimate);
    $("confirm-fill").onclick = async function () {
      var values; try { values = estimate(); } catch (e) { setStatus("fill-status", W.error(e), true); return; }
      $("confirm-fill").disabled = true;
      await operation("fill-status", async function (ctx, say) {
        if (mk !== ctx.key) throw new Error("交易对已变化，请重新打开订单");
        if (ctx.who === o.maker) throw new Error("不能成交自己的挂单，请使用撤单");
        var fresh = await lookupOrder(id); if (!fresh.open || fresh.expiry <= Date.now() / 1000 || fresh.giveLeft < values.amount) throw new Error("订单已变化，请刷新后重试");
        await ensureAllowance(ctx, payToken, values.pay, say);
        await send(ctx, ctx.m.escrow, SEL.fill + word(id) + word(values.amount) + word(values.pay), say, "成交订单 #" + id);
        dialog.close(); toast("订单 #" + id + " 成交已在链上确认");
      });
      if ($("confirm-fill")) $("confirm-fill").disabled = false;
    };
  }
  function refundData(ids) { return SEL.refund + word(32) + word(ids.length) + ids.map(word).join(""); }
  async function cancelOrder(id) {
    if (busy || !verified) return;
    var mk = marketKey, o; try { o = await lookupOrder(id); } catch (e) { toast(W.error(e)); return; }
    if (mk !== marketKey) return;
    if (!o.open || !o.giveLeft) { toast("订单已结清，请刷新"); return; }
    var token = o.sellFlgns ? C.tokenA : market(), expired = o.expiry <= Date.now() / 1000;
    var dialog = W.dialog(expired ? "取回到期订单" : "撤销挂单", '<p>订单 #' + id + ' 剩余的 <b>' + esc(tokenDisplay(o.giveLeft, token)) + ' ' + esc(token.symbol) + '</b> 将退回原挂单钱包 ' + esc(short(o.maker)) + '。</p><button class="primary wide" id="confirm-cancel">确认' + (expired ? "取回" : "撤单") + '</button><p class="operation-status" id="cancel-status" role="status"></p>');
    $("confirm-cancel").onclick = async function () {
      $("confirm-cancel").disabled = true;
      await operation("cancel-status", async function (ctx, say) {
        if (ctx.key !== mk) throw new Error("交易对已变化，请重新打开订单");
        if (ctx.who !== o.maker && !expired) throw new Error("只有挂单方可以撤销未到期订单");
        await send(ctx, ctx.m.escrow, expired ? refundData([id]) : SEL.cancel + word(id), say, (expired ? "取回" : "撤销") + "订单 #" + id);
        dialog.close(); toast("订单 #" + id + " 退款已在链上确认");
      });
      if ($("confirm-cancel")) $("confirm-cancel").disabled = false;
    };
  }
  async function refundAll() {
    var mk = marketKey;
    var ids = orders.filter(function (o) { return o.maker === account && o.open && o.giveLeft > 0n && o.expiry <= Date.now() / 1000; }).map(function (o) { return o.id; });
    if (!ids.length) return;
    var d = W.dialog("取回到期订单", '<p>将取回当前交易对的全部 ' + ids.length + ' 笔已到期未结清订单。每笔交易最多处理 25 单，共需 ' + Math.ceil(ids.length / 25) + ' 次钱包确认。资金退回你的原钱包。</p><button id="confirm-refund-all" class="primary wide">确认取回</button><p class="operation-status" id="refund-status" role="status"></p>');
    $("confirm-refund-all").onclick = async function () {
      $("confirm-refund-all").disabled = true;
      await operation("refund-status", async function (ctx, say) {
        if (ctx.key !== mk) throw new Error("交易对已变化，请重新打开到期订单");
        for (var offset = 0; offset < ids.length; offset += 25) await send(ctx, ctx.m.escrow, refundData(ids.slice(offset, offset + 25)), say, "到期取回 " + (Math.floor(offset / 25) + 1) + "/" + Math.ceil(ids.length / 25));
        d.close(); toast("到期订单已取回，链上已确认");
      });
      if ($("confirm-refund-all")) $("confirm-refund-all").disabled = false;
    };
  }
  async function runLookup() {
    if (!verified) { toast("市场尚未开放"); return; }
    var text = $("lookup-id").value.trim(), mk = marketKey;
    if (!/^\d+$/.test(text) || BigInt(text) > BigInt(Number.MAX_SAFE_INTEGER)) { $("lookup-result").textContent = "请输入有效的整数订单编号"; return; }
    $("lookup-result").textContent = "正在读取订单…";
    try {
      var o = await lookupOrder(Number(text)); if (mk !== marketKey) return;
      // Keep found orders accessible even if a previous full scan predates creation.
      if (!orders.some(function (x) { return x.id === o.id; })) orders.push(o);
      var token = o.sellFlgns ? C.tokenA : market();
      $("lookup-result").innerHTML = '<div class="lookup-result"><b>订单 #' + o.id + ' · ' + (o.sellFlgns ? "卖出" : "买入") + ' FLGNS</b><p>挂单方：' + addressLink(o.maker) + '</p><p>托管剩余：' + esc(tokenDisplay(o.giveLeft, token)) + ' ' + esc(token.symbol) + '</p><p>状态：' + esc(timeLeft(o)) + ' · 到期 ' + esc(new Date(o.expiry * 1000).toLocaleString("zh-CN")) + '</p><div class="action-line">' + button(o) + '</div></div>';
    } catch (e) { if (mk === marketKey) $("lookup-result").textContent = "查询失败：" + W.error(e); }
  }
  document.addEventListener("click", function (event) {
    var b = event.target.closest("[data-market],[data-goform],[data-side],[data-page],[data-fill],[data-cancel],[data-refund]"); if (!b) return;
    if (b.hasAttribute("data-market")) { if (busy || b.dataset.market === marketKey) return; location.hash = b.dataset.market; }
    else if (b.hasAttribute("data-goform")) { if (busy) return; updateSide(b.dataset.goform); $("create-order").scrollIntoView({ behavior: "smooth" }); $("flgns-amount").focus({ preventScroll: true }); }
    else if (b.hasAttribute("data-side")) { if (!busy) updateSide(b.dataset.side); }
    else if (b.hasAttribute("data-page")) { pages[b.dataset.page] += Number(b.dataset.step); renderBooks(); }
    else if (b.hasAttribute("data-fill")) fillDialog(Number(b.dataset.fill));
    else if (b.hasAttribute("data-cancel")) cancelOrder(Number(b.dataset.cancel));
    else if (b.hasAttribute("data-refund")) cancelOrder(Number(b.dataset.refund));
  });
  window.addEventListener("hashchange", function () { var next = location.hash.slice(1); if (!keys.includes(next)) return; if (busy) { history.replaceState(null, "", "#" + marketKey); toast("请先完成当前钱包操作"); return; } marketKey = next; $("flgns-amount").value = ""; $("quote-amount").value = ""; setStatus("create-status", "", false); updateSide(side); refresh(); updateWallet(); });
  $("connect-wallet").onclick = function () { connect().catch(function () {}); };
  $("wallet-card-connect").onclick = function () { connect().catch(function () {}); };
  $("refresh-book").onclick = refresh;
  $("create-form").onsubmit = createOrder;
  $("flgns-amount").oninput = previewPrice; $("quote-amount").oninput = previewPrice;
  $("mine-filter").onchange = function () { pages.mine = 0; renderMine(); };
  $("refund-expired").onclick = refundAll; $("lookup-order").onclick = runLookup;
  $("lookup-id").onkeydown = function (e) { if (e.key === "Enter") runLookup(); };
  W.onAccounts(function (accounts) {
    account = accounts && /^0x[0-9a-f]{40}$/i.test(accounts[0]) ? accounts[0].toLowerCase() : null;
    if (!account) { $("connect-wallet").textContent = "连接钱包"; $("wallet-details").innerHTML = '<div class="empty">钱包已断开，请重新连接</div>'; }
    else updateWallet(); renderBooks(); controls();
  });
  W.onChain(function (chainId) { if (BigInt(chainId) !== BigInt(C.chain.id)) toast("钱包当前网络不是 Anubis；进行交易时将请求切换网络"); });
  W.silent().then(function (a) { if (a) { account = a; updateWallet(); renderBooks(); controls(); } });
  updateSide("sell"); refresh();
})();
