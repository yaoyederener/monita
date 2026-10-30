// Cloudflare 定时器：每分钟运行一次，按时间表触发 monita 的 GitHub Actions。
// GitHub 自带的定时任务被严重限流（会延迟几个小时甚至不跑），手动触发（workflow_dispatch）不受影响。
// 需要在 Worker 里设置加密变量 GH_TOKEN：只授权 yaoyederener/monita 仓库、Actions 读写权限的 GitHub 令牌。
const REPO = "yaoyederener/monita";

// [workflow 文件, 是否在这一分钟触发(UTC 时间), 传给 workflow 的参数]
const JOBS = [
  ["otc-refund-keeper.yml", (t) => t.getUTCMinutes() % 5 === 0],
  ["ailgns-flow-history.yml", (t) => t.getUTCMinutes() === 23],
  ["ailgns-lp-monitor.yml", (t) => t.getUTCMinutes() === 0 && (t.getUTCHours() === 1 || t.getUTCHours() === 13)],
  ["eth-gap-alert.yml", (t) => t.getUTCMinutes() === 0 && t.getUTCHours() % 6 === 0, { gap: "20", size: "10", test: "false" }],
];

async function dispatch(env, file, inputs) {
  const r = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${file}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "ailgns-cron-worker",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({ ref: "main", inputs: inputs || {} }),
  });
  return `${file}: ${r.status}${r.ok ? "" : " " + (await r.text()).slice(0, 200)}`;
}

async function run(env, time) {
  const due = JOBS.filter(([, when]) => when(time));
  return Promise.all(due.map(([file, , inputs]) => dispatch(env, file, inputs)));
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(run(env, new Date(event.scheduledTime)).then((r) => r.length && console.log(r.join(" | "))));
  },
  // 打开 Worker 网址可以看到下一次各任务的触发安排，不会触发任何任务
  async fetch() {
    const lines = JOBS.map(([file]) => file);
    return new Response("ailgns cron worker\n" + lines.join("\n") + "\n", { headers: { "content-type": "text/plain; charset=utf-8" } });
  },
};
