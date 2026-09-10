const {
  ACTIONS_ID_TOKEN_REQUEST_TOKEN,
  ACTIONS_ID_TOKEN_REQUEST_URL,
  BOT_TOKEN,
  CHAT_ID,
  BSC_RPC,
  BSCSCAN_KEY,
  WORKER_URL,
} = process.env;

for (const [name, value] of Object.entries({
  ACTIONS_ID_TOKEN_REQUEST_TOKEN,
  ACTIONS_ID_TOKEN_REQUEST_URL,
  BOT_TOKEN,
  CHAT_ID,
  BSC_RPC,
  WORKER_URL,
})) {
  if (!value) throw new Error(`Missing required environment value: ${name}`);
}

const oidcResponse = await fetch(
  `${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=${encodeURIComponent("laptop-base-pool-monitor")}`,
  { headers: { authorization: `Bearer ${ACTIONS_ID_TOKEN_REQUEST_TOKEN}` } },
);
if (!oidcResponse.ok) throw new Error(`GitHub OIDC request failed: HTTP ${oidcResponse.status}`);
const { value: oidcToken } = await oidcResponse.json();
if (!oidcToken) throw new Error("GitHub OIDC response did not contain a token");

const authorization = { authorization: `Bearer ${oidcToken}` };
const configureResponse = await fetch(`${WORKER_URL}/bootstrap`, {
  method: "POST",
  headers: { ...authorization, "content-type": "application/json" },
  body: JSON.stringify({ botToken: BOT_TOKEN, chatId: CHAT_ID, bscRpc: BSC_RPC, bscscanKey: BSCSCAN_KEY || "" }),
});
if (!configureResponse.ok) throw new Error(`Worker configuration failed: HTTP ${configureResponse.status}`);
console.log("Worker credentials configured without exposing secret values.");

const runResponse = await fetch(`${WORKER_URL}/run`, { method: "POST", headers: authorization });
const runResult = await runResponse.json();
if (!runResponse.ok) throw new Error(`Initial monitor run failed: ${runResult.error || runResponse.status}`);
console.log(`Initial scan complete: scannedThrough=${runResult.scannedThrough}, remainingBlocks=${runResult.remainingBlocks}`);
