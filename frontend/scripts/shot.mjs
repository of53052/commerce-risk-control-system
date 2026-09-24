/**
 * 视觉自检脚本：用 Edge 无头浏览器 + CDP 给页面截图（仅开发期使用，不参与构建）。
 *
 * 为什么不用 `msedge --screenshot` 一次性截屏：主框架页面需要已登录会话，
 * 而会话存在 localStorage 里，必须先注入 token 再刷新，一次性命令做不到。
 * 因此改成 CDP 长连接：导航 → 注入 localStorage → 重载 → 截屏。
 *
 * 用法（先确保 vite dev server 在 5173、后端在 8000）：
 *   node scripts/shot.mjs            # 默认截 login + dashboard + workbench + policy + simulation
 *   node scripts/shot.mjs dashboard  # 只截指定页面
 */
import { spawn } from "node:child_process";
import { accessSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

// 连接同一 CDP 端口的多个页面会共享 storage 分区：localhost 和 127.0.0.1 是不同源，
// 即使代码都写 localhost，跨场景（不同端口连过 127.0.0.1 的 CDP 客户端）也会互相串。
// 手动核对用哪边访问，就把 DEV_BASE 改成哪边，保持源一致。
const BASE = process.env.DEV_BASE ?? "http://localhost:5173";
const API = "http://127.0.0.1:8000";
const OUT_DIR = join(import.meta.dirname, "..", "..", "scripts", "_shots");
const PROFILE = join(tmpdir(), `risk-shot-${Date.now()}`);
const PORT = 9333;
const VIEWPORT = { width: 1600, height: 1000 };

const EDGE_CANDIDATES = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 登录取 token：截图需要真实会话，不能伪造 userId 骗过后端鉴权。 */
async function login() {
  const resp = await fetch(`${API}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: "admin", password: "admin123" }),
  });
  if (!resp.ok) throw new Error(`登录失败 HTTP ${resp.status}`);
  const body = await resp.json();
  const data = body.data ?? body;
  // 后端 TokenOut 用的是 access_token（OAuth2 风格），不是 token；
  // 字段名写错会让 token 变成 undefined，注入后应用仍停在登录页。
  return { token: data.access_token, user: data.user };
}

/** 极简 CDP 客户端：够用即可，不引第三方依赖（Node 22+ 自带 WebSocket）。 */
function connectCdp(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let seq = 0;
  const pending = new Map();
  const events = new Map();

  const ready = new Promise((resolve, reject) => {
    ws.addEventListener("open", () => resolve());
    ws.addEventListener("error", (e) => reject(new Error(`CDP 连接失败: ${e.type}`)));
  });

  ws.addEventListener("message", (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(msg.error.message));
      else resolve(msg.result);
      return;
    }
    const handlers = events.get(msg.method);
    if (handlers) handlers.forEach((fn) => fn(msg.params));
  });

  return {
    ready,
    send(method, params = {}) {
      const id = ++seq;
      ws.send(JSON.stringify({ id, method, params }));
      return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
    },
    once(method, timeoutMs = 15000) {
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(`等待事件超时: ${method}`)), timeoutMs);
        const list = events.get(method) ?? [];
        const fn = (params) => {
          clearTimeout(timer);
          events.set(method, (events.get(method) ?? []).filter((f) => f !== fn));
          resolve(params);
        };
        list.push(fn);
        events.set(method, list);
      });
    },
    close() {
      ws.close();
    },
  };
}

function findEdge() {
  for (const p of EDGE_CANDIDATES) {
    try {
      accessSync(p);
      return p;
    } catch {
      /* 换下一个候选路径 */
    }
  }
  throw new Error("未找到 msedge.exe");
}

async function waitForDevtools() {
  for (let i = 0; i < 60; i++) {
    try {
      const resp = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const targets = await resp.json();
      const page = targets.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (page) return page.webSocketDebuggerUrl;
    } catch {
      /* 浏览器还没起来，继续轮询 */
    }
    await sleep(250);
  }
  throw new Error("等待 DevTools 端口超时");
}

/** 每个页面的截图编排：navigate → 可选注入会话 → 重载 → 等渲染 → 截屏。 */
const SCENES = {
  login: { path: "/login", auth: false, waitMs: 1500 },
  dashboard: { path: "/dashboard", auth: true, waitMs: 2500 },
  workbench: { path: "/workbench", auth: true, waitMs: 2500 },
  policy: { path: "/policy", auth: true, waitMs: 2500 },
  simulation: { path: "/simulation", auth: true, waitMs: 2500 },
};

async function main() {
  const wanted = process.argv.slice(2).filter((a) => a in SCENES);
  const scenes = wanted.length ? wanted : Object.keys(SCENES);

  mkdirSync(OUT_DIR, { recursive: true });
  const session = await login();
  const edge = findEdge();

  const proc = spawn(
    edge,
    [
      "--headless=new",
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${PROFILE}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-extensions",
      "--hide-scrollbars",
      `--window-size=${VIEWPORT.width},${VIEWPORT.height}`,
      "about:blank",
    ],
    { stdio: "ignore" }
  );

  const cdp = connectCdp(await waitForDevtools());
  try {
    await cdp.ready;
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      ...VIEWPORT,
      deviceScaleFactor: 1,
      mobile: false,
    });

    for (const name of scenes) {
      const scene = SCENES[name];
      const loaded = cdp.once("Page.loadEventFired");
      await cdp.send("Page.navigate", { url: `${BASE}${scene.path}` });
      await loaded;
      await sleep(400);

      if (scene.auth) {
        // zustand/persist 的存储形态：{ state: {...}, version: 0 }，key 固定为 risk-auth
        const payload = JSON.stringify({ state: { token: session.token, user: session.user }, version: 0 });
        const reloaded = cdp.once("Page.loadEventFired");
        await cdp.send("Runtime.evaluate", {
          expression: `localStorage.setItem("risk-auth", ${JSON.stringify(payload)}); location.href = ${JSON.stringify(BASE + scene.path)};`,
        });
        await reloaded;
      }

      await sleep(scene.waitMs);
      // 打印首屏可见文字，便于在不看图的情况下确认渲染的是哪一页（截图人工审核的补充手段）
      const { result } = await cdp.send("Runtime.evaluate", {
        // include 脚本 diag：打印 location/会话是否可读，便于定位“会话注了但仍在登录页”这类问题
        expression: `location.pathname + " || token=" + !!localStorage.getItem("risk-auth") + " || " + document.body.innerText.replace(/\\n+/g, " | ").slice(0, 160)`,
        returnByValue: true,
      });
      console.log(`[shot] ${name} text: ${result.value}`);
      const { data } = await cdp.send("Page.captureScreenshot", { format: "png" });
      const file = join(OUT_DIR, `${name}.png`);
      writeFileSync(file, Buffer.from(data, "base64"));
      console.log(`[shot] ${name} -> ${file}`);
    }
  } finally {
    cdp.close();
    proc.kill();
    await sleep(500);
    rmSync(PROFILE, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error(`[shot] 失败: ${err.message}`);
  process.exit(1);
});
