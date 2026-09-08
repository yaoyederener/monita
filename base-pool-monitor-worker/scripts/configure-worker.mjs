const { ACTIONS_ID_TOKEN_REQUEST_TOKEN, ACTIONS_ID_TOKEN_REQUEST_URL, BOT_TOKEN, CHAT_ID, WORKER_URL } =
  process.env;

for (const [name, value] of Object.entries({
  ACTIONS_ID_TOKEN_REQUEST_TOKEN,
  ACTIONS_ID_TOKEN_REQUEST_URL,
  BOT_TOKEN,
  CHAT_ID,
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

const configureResponse = await fetch(`${WORKER_URL}/bootstrap`, {
  method: "POST",
  headers: {
    authorization: `Bearer ${oidcToken}`,
    "content-type": "application/json",
  },
  body: JSON.stringify({ botToken: BOT_TOKEN, chatId: CHAT_ID }),
});
if (!configureResponse.ok) {
  const detail = await configureResponse.text();
  throw new Error(`Worker configuration failed: HTTP ${configureResponse.status}: ${detail}`);
}
console.log("Worker Telegram credentials configured successfully.");
