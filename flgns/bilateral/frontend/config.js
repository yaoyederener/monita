/* Deployment values are populated only after on-chain verification. */
window.FLGNS_CONFIG = {
  deploymentReady: true,
  chain: {
    id: 6714,
    name: "Anubis",
    nativeCurrency: { name: "DAI", symbol: "DAI", decimals: 18 },
    rpcUrls: ["https://rpc.anubispace.org", "https://rpc2.anubispace.org", "https://rpc3.anubispace.org"],
    explorer: "https://anubisscan.io"
  },
  tokenA: { symbol: "FLGNS", address: "0xffffbe730e2cde54d8928d36f547e41deb66aaaa", decimals: 18 },
  markets: {
    dai: { symbol: "DAI", address: "0x83fd06f0846d9d90b3016bf670efe2e0b11cde14", decimals: 18, escrow: "0xc4ea53316a3e16d8b6d34c6c24234af6660e7339" },
    lgns: { symbol: "LGNS", address: "0x4D1D808a081FdAc440703b3765FC61f8028C06B8", decimals: 9, escrow: "0xf0330d8cca86a378387c4614fd45feb365de4046" },
    ailgns: { symbol: "AiLGNS", address: "0x256edb652db43e721ec6a12b6b51b6967d3bb8cb", decimals: 18, escrow: "0x738f6bc29a4c17c2b723e51edacd953910591196" },
    rm: { symbol: "RM", address: "0x2e045e1380f5f5b6e7550328910a7377c9fce59d", decimals: 9, escrow: "0x26cb1272f4fa0f54a9208a31391b983618cc3001" }
  },
  feeBps: 40,
  feeRecipient60: "0xd07Fe622d0D1628780853b31C21B9ABAf96B9c00",
  feeRecipient40: "0xedb9c6900a11d2dd37da6b59f3604c35a39db1e6",
  sourceUrl: "https://github.com/yaoyederener/monita/blob/f6a97edf95342798160814b310ce035bc2a985c1/flgns/bilateral/contracts/FlgnsOtcEscrowV2.sol",
  deploymentUrl: "https://github.com/yaoyederener/monita/actions/runs/41398330401"
};
