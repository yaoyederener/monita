(function () {
  "use strict";
  var W = window.FLGNSWallet, N = window.FLGNSAmounts, esc = W.esc;
  var SOURCE = "0xedb9c6900a11d2dd37da6b59f3604c35a39db1e6", SPLITTER = "0xeee877c1b6285b82890bdcd027d2715f658ecf23";
  var PAYEE1 = "0xe8782615e32557f257dd88d70b1eb0d7f47b91c3", PAYEE2 = "0xd53a00bb90bb606f10d888419a14eca779f17285", KEEPER = "0x89dd06754bb239b6c4c92bc47c8fa72889084e27";
  var TOKENS = [{ key: "flgns", symbol: "FLGNS", address: "0xffffbe730e2cde54d8928d36f547e41deb66aaaa", decimals: 18 }, { key: "rm", symbol: "RM", address: "0x2e045e1380f5f5b6e7550328910a7377c9fce59d", decimals: 9 }];
  var RPCS = ["https://rpc2.anubispace.org", "https://rpc.anubispace.org", "https://rpc3.anubispace.org"], EXPLORER = "https://anubisscan.io", MAX = (1n << 256n) - 1n;
  var SEL = { source: "0xf230b4c2", payee1: "0xbb965143", payee2: "0x31f009a1", payee3: "0xe6012d30", bps1: "0x8edd1de8", bps2: "0xdb52e2e7", bps3: "0x63815352", keeper: "0xaced1661", decimals: "0x313ce567", allowance: "0xdd62ed3e", balance: "0x70a08231", approve: "0x095ea7b3" };
  var account = null, verified = false, busy = false, balances = {}, allowances = {}, epoch = 0;
  function $(id) { return document.getElementById(id); }
  function word(v) { var s = BigInt(v).toString(16); if (s.startsWith("-") || s.length > 64) throw new Error("额度超出合约范围"); return s.padStart(64, "0"); }
  function integer(h) { if (!/^0x[0-9a-f]{64}$/i.test(h)) throw new Error("链上数值返回格式无效"); return BigInt(h); }
  function address(h) { if (!/^0x0{24}[0-9a-f]{40}$/i.test(h)) throw new Error("链上地址返回格式无效"); return "0x" + h.slice(-40).toLowerCase(); }
  function link(a) { return '<a class="mono" target="_blank" rel="noopener" href="' + EXPLORER + '/address/' + a + '">' + a + ' ↗</a>'; }
  function state(message, type) { $("setup-state").textContent = message; $("setup-state").className = "status-banner" + (type ? " " + type : ""); }
  function status(t, message, bad) { var el = $("status-" + t.key); el.textContent = message; el.classList.toggle("error", !!bad); }
  function controls() {
    $("connect-wallet").disabled = busy; $("recheck").disabled = busy;
    TOKENS.forEach(function (t) {
      $("approve-" + t.key).disabled = busy || !verified || account !== SOURCE;
      $("zero-" + t.key).disabled = busy || !verified || account !== SOURCE || !allowances[t.key];
      $("max-" + t.key).disabled = busy;
      $("amount-" + t.key).disabled = busy || $("max-" + t.key).checked;
    });
  }
  async function rpc(method, params) {
    var last;
    for (var i = 0; i < RPCS.length; i++) {
      var abort = new AbortController(), timer = setTimeout(function () { abort.abort(); }, 12000);
      try {
        var response = await fetch(RPCS[i], { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: Date.now(), method: method, params: params || [] }), signal: abort.signal });
        if (!response.ok) throw new Error("节点返回 HTTP " + response.status);
        var result = await response.json(); if (result.error) throw new Error(result.error.message || "链上查询失败");
        if (result.result === undefined) throw new Error("节点未返回结果"); return result.result;
      } catch (e) { last = e; } finally { clearTimeout(timer); }
    }
    throw last || new Error("链上节点不可用");
  }
  function call(to, data, block) { return rpc("eth_call", [{ to: to, data: data }, block || "latest"]); }
  function renderBalances() {
    TOKENS.forEach(function (t) {
      var current = allowances[t.key];
      $("stats-" + t.key).innerHTML = '<div>SOURCE 余额：<b>' + (balances[t.key] === undefined ? "读取中…" : esc(N.format(balances[t.key], t.decimals, true)) + ' ' + t.symbol) + '</b></div><div>当前授权额度：<b>' + (current === undefined ? "读取中…" : current === MAX ? "最大额度" : esc(N.format(current, t.decimals, true)) + ' ' + t.symbol) + '</b></div>';
    }); controls();
  }
  async function verify() {
    var id = ++epoch; verified = false; controls(); state("正在只读核对链上配置…");
    try {
      if (BigInt(await rpc("eth_chainId", [])) !== 6714n) throw new Error("RPC 网络不是 Anubis 6714");
      var block = await rpc("eth_blockNumber", []);
      var checks = await Promise.all([rpc("eth_getCode", [SPLITTER, block]), call(SPLITTER, SEL.source, block), call(SPLITTER, SEL.payee1, block), call(SPLITTER, SEL.payee2, block), call(SPLITTER, SEL.payee3, block), call(SPLITTER, SEL.bps1, block), call(SPLITTER, SEL.bps2, block), call(SPLITTER, SEL.bps3, block), call(SPLITTER, SEL.keeper, block)]);
      if (!/^0x[0-9a-f]{2,}$/i.test(checks[0]) || address(checks[1]) !== SOURCE || address(checks[2]) !== PAYEE1 || address(checks[3]) !== PAYEE2 || address(checks[4]) !== SOURCE || integer(checks[5]) !== 3000n || integer(checks[6]) !== 4000n || integer(checks[7]) !== 3000n || address(checks[8]) !== KEEPER) throw new Error("固定分账地址、比例或 keeper 与预期不一致");
      var data = await Promise.all(TOKENS.map(async function (t) {
        var values = await Promise.all([rpc("eth_getCode", [t.address, block]), call(t.address, SEL.decimals, block), call(t.address, SEL.balance + word(SOURCE), block), call(t.address, SEL.allowance + word(SOURCE) + word(SPLITTER), block)]);
        if (!/^0x[0-9a-f]{2,}$/i.test(values[0]) || integer(values[1]) !== BigInt(t.decimals)) throw new Error(t.symbol + " 合约或精度核对失败");
        return { key: t.key, balance: integer(values[2]), allowance: integer(values[3]) };
      }));
      if (id !== epoch) return;
      data.forEach(function (x) { balances[x.key] = x.balance; allowances[x.key] = x.allowance; }); verified = true; renderBalances();
      state("链上配置已核对 · 区块 " + BigInt(block).toString() + (account === SOURCE ? " · SOURCE 钱包已连接，可选择授权额度" : account ? " · 当前钱包不是 SOURCE，无法授权" : " · 请连接 SOURCE 钱包"), account && account !== SOURCE ? "pending" : "");
    } catch (e) { if (id === epoch) { verified = false; state("核对失败：" + W.error(e) + "。授权已禁用。", "error"); } }
    finally { controls(); }
  }
  async function assertSource() {
    var provider = W.get(); if (!provider || account !== SOURCE) throw new Error("只有固定 SOURCE 钱包可以授权");
    await W.switchChain(provider);
    var accounts = await provider.request({ method: "eth_accounts" });
    if (!accounts || !accounts[0] || accounts[0].toLowerCase() !== SOURCE || BigInt(await provider.request({ method: "eth_chainId" })) !== 6714n) throw new Error("钱包或网络已变化，请重新连接 SOURCE 钱包");
    return provider;
  }
  async function confirmReceipt(hash, t) {
    for (var i = 0; i < 180; i++) {
      var receipt = await rpc("eth_getTransactionReceipt", [hash]);
      if (receipt) { if (BigInt(receipt.status) !== 1n) throw new Error("链上交易已回退，授权未完成"); return; }
      await new Promise(function (resolve) { setTimeout(resolve, 2000); });
    }
    throw new Error("交易仍待确认，请先核对交易记录，不要重复提交");
  }
  async function approve(t, zero) {
    if (busy || !verified || account !== SOURCE) return;
    var amount;
    try { amount = zero ? 0n : $("max-" + t.key).checked ? MAX : N.parse($("amount-" + t.key).value, t.decimals); if (!zero && !amount) throw new Error("请填写大于 0 的授权额度；撤销请使用清零授权"); }
    catch (e) { status(t, W.error(e), true); return; }
    busy = true; controls(); var hash;
    status(t, "正在重新核对分账配置与钱包…");
    try {
      await verify(); if (!verified) throw new Error("链上配置未通过核对");
      var provider = await assertSource(), tx = { from: SOURCE, to: t.address, data: SEL.approve + word(SPLITTER) + word(amount) };
      status(t, "正在模拟 " + (zero ? "清零" : "设置") + " " + t.symbol + " 授权…");
      await rpc("eth_call", [tx, "latest"]);
      tx.gas = "0x" + (BigInt(await rpc("eth_estimateGas", [tx])) * 13n / 10n).toString(16);
      await assertSource(); status(t, "请在钱包确认：" + (amount === MAX ? "最大额度" : N.format(amount, t.decimals, true) + " " + t.symbol) + "，授权对象 " + SPLITTER);
      hash = await provider.request({ method: "eth_sendTransaction", params: [tx] });
      if (!/^0x[0-9a-f]{64}$/i.test(hash)) throw new Error("钱包未返回有效交易哈希，请先核对钱包记录");
      status(t, "已提交，等待链上确认：" + hash);
      await confirmReceipt(hash, t);
      var actual = integer(await call(t.address, SEL.allowance + word(SOURCE) + word(SPLITTER)));
      if (actual !== amount) throw new Error("交易已确认，但当前授权额度与本次设置不同，请重新核对后再操作");
      status(t, (zero ? "清零" : "授权") + "已链上确认：" + hash); await verify();
    } catch (e) { status(t, (hash ? "交易 " + hash + "。请先核对区块浏览器，不要重复提交。" : "") + W.error(e), true); }
    finally { busy = false; controls(); }
  }
  $("split-config").innerHTML = '<div><span>授权对象 · 既有分账合约</span>' + link(SPLITTER) + '</div><div><span>必须连接的 SOURCE 钱包 · 留存 30%</span>' + link(SOURCE) + '</div><div class="contract-grid"><div><span>PAYEE1 · 30%</span>' + link(PAYEE1) + '</div><div><span>PAYEE2 · 40%</span>' + link(PAYEE2) + '</div></div><div><span>固定 keeper · 触发分账并支付 Gas</span>' + link(KEEPER) + '</div>';
  $("setup-tokens").innerHTML = TOKENS.map(function (t) { return '<section class="card"><div class="card-head"><h2>' + t.symbol + ' 授权</h2><span class="muted">' + t.decimals + ' 位精度</span></div><p>' + link(t.address) + '</p><div class="token-stats" id="stats-' + t.key + '">正在读取 SOURCE 余额与授权…</div><label class="field"><span>自定义授权额度</span><div class="input-wrap"><input inputmode="decimal" autocomplete="off" placeholder="输入代币数量，不是美元金额" id="amount-' + t.key + '"><b>' + t.symbol + '</b></div></label><label class="choice"><input type="checkbox" id="max-' + t.key + '"><span>我明确选择最大额度（直到撤销前有效）</span></label><div class="buttons"><button id="approve-' + t.key + '" disabled>设置 ' + t.symbol + ' 授权</button><button id="zero-' + t.key + '" class="secondary" disabled>清零 ' + t.symbol + ' 授权</button></div><p class="operation-status" id="status-' + t.key + '" role="status"></p></section>'; }).join("");
  TOKENS.forEach(function (t) { $("approve-" + t.key).onclick = function () { approve(t, false); }; $("zero-" + t.key).onclick = function () { approve(t, true); }; $("max-" + t.key).onchange = controls; });
  $("recheck").onclick = verify;
  $("connect-wallet").onclick = async function () { if (busy) return; busy = true; controls(); try { account = await W.connect(); $("connect-wallet").textContent = account.slice(0, 6) + "…" + account.slice(-4); await verify(); } catch (e) { state(W.error(e), "error"); } finally { busy = false; controls(); } };
  W.onAccounts(function (accounts) { account = accounts && accounts[0] ? accounts[0].toLowerCase() : null; $("connect-wallet").textContent = account ? account.slice(0, 6) + "…" + account.slice(-4) : "连接 SOURCE 钱包"; controls(); if (!busy) verify(); });
  W.onChain(function (chain) { if (BigInt(chain) !== 6714n) { state("钱包当前网络不是 Anubis，提交时必须切换并重新核对。", "pending"); } });
  verify();
})();
