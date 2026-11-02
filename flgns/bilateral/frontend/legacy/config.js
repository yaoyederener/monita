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
    dai: { symbol: "DAI", address: "0x83fd06f0846d9d90b3016bf670efe2e0b11cde14", decimals: 18, escrow: "0x79308e458a84070caeb10de89914f7f1eace081c" },
    lgns: { symbol: "LGNS", address: "0x4D1D808a081FdAc440703b3765FC61f8028C06B8", decimals: 9, escrow: "0x7bc2ba0f3189ef0c39385436d5a272f360900714" },
    ailgns: { symbol: "AiLGNS", address: "0x256edb652db43e721ec6a12b6b51b6967d3bb8cb", decimals: 18, escrow: "0xd18d48a28345d948d18b7840a5b33dced56f7eab" },
    rm: { symbol: "RM", address: "0x2e045e1380f5f5b6e7550328910a7377c9fce59d", decimals: 9, escrow: "0x52105b21ac346e69d4eb7259b270238280ecc069" }
  },
  feeBps: 200,
  feeRecipient60: "0xd07Fe622d0D1628780853b31C21B9ABAf96B9c00",
  feeRecipient40: "0xedb9c6900a11d2dd37da6b59f3604c35a39db1e6",
  sourceUrl: "https://github.com/yaoyederener/monita/blob/3aa1f331b9fef1f65c4d2519e2f13f45cd6c0e0b/flgns/contracts/FlgnsOtcEscrow.sol",
  deploymentUrl: "https://github.com/yaoyederener/monita/actions/runs/41382088294"
};
