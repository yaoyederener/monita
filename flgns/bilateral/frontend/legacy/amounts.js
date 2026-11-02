(function (root) {
  "use strict";
  function parse(value, decimals) {
    if (!Number.isInteger(decimals) || decimals < 0 || decimals > 36) throw new Error("代币精度尚未验证");
    var s = String(value).trim();
    if (!/^(?:\d+|[1-9]\d{0,2}(?:,\d{3})+)(?:\.\d+)?$/.test(s)) throw new Error("请输入明确的数字，例如 1000.25 或 1,000.25；不支持逗号小数、空格或科学计数法");
    var parts = s.replace(/,/g, "").split("."), fraction = parts[1] || "";
    if (fraction.length > decimals) throw new Error("最多支持 " + decimals + " 位小数，请明确修改金额");
    var result = BigInt(parts[0]) * 10n ** BigInt(decimals) + BigInt(fraction.padEnd(decimals, "0") || "0");
    if (result >= 2n ** 256n) throw new Error("金额超出合约允许范围");
    return result;
  }
  function format(value, decimals, grouping) {
    var n = BigInt(value), unit = 10n ** BigInt(decimals), whole = (n / unit).toString();
    if (grouping) whole = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    var fraction = decimals ? (n % unit).toString().padStart(decimals, "0").replace(/0+$/, "") : "";
    return whole + (fraction ? "." + fraction : "");
  }
  function ceilDiv(a, b) { if (b <= 0n) throw new Error("无效订单价格"); return (a + b - 1n) / b; }
  function ratio(numerator, denominator, numeratorDecimals, denominatorDecimals, places) {
    if (!denominator) return "—";
    places = places === undefined ? 10 : places;
    var n = numerator * 10n ** BigInt(denominatorDecimals + places) / (denominator * 10n ** BigInt(numeratorDecimals));
    return format(n, places, true) || "0";
  }
  var api = { parse: parse, format: format, ceilDiv: ceilDiv, ratio: ratio };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FLGNSAmounts = api;
})(typeof window === "object" ? window : globalThis);
