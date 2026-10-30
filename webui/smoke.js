// WebUI 冒烟：把 index.html 里的内联脚本放进 DOM 桩里真跑一遍。
//
// 为什么需要：前端出错只会表现为"页面某个按钮没反应"，或者像上次那样
// `ReferenceError: renderDetail is not defined` —— 语法检查抓不到（语法是合法的），
// 只有真执行到那行才炸。这个脚本在 Node 里执行整段脚本 + 初始化链，跑完检查表格渲染。
//
//     node webui/smoke.js
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
if (!blocks.length) {
  console.error("✖ index.html 里没有 <script>");
  process.exit(1);
}
const code = blocks[blocks.length - 1][1];

const nodes = new Map();
function makeEl(id) {
  const el = {
    id,
    innerHTML: "",
    textContent: "",
    title: "",
    value: "",
    disabled: false,
    scrollTop: 0,
    scrollHeight: 0,
    dataset: {},
    style: { setProperty() {}, removeProperty() {}, cursor: "" },
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      toggle(c, on) { if (on === undefined ? !this._s.has(c) : on) this._s.add(c); else this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    addEventListener() {},
    removeEventListener() {},
    appendChild() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    focus() {},
    closest() { return null; },
    getBoundingClientRect() { return { width: 120 }; },
  };
  return el;
}
const document = {
  getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, makeEl(id));
    return nodes.get(id);
  },
  querySelector() { return makeEl("q"); },
  querySelectorAll() { return []; },
  addEventListener() {},
  body: makeEl("body-el"),
  documentElement: makeEl("root"),
  createElement() { return makeEl("tmp"); },
};

const agents = [
  { id: "gemini", label: "Gemini CLI（Google）", mode: "flag", exists: true, enabled: true,
    ready: true, needs_login: false, command: "gemini -p", source: "path",
    aliases: ["gcli"], version: "0.65.0", install_cmd: "bun add -g @google/gemini-cli", notes: "", login_hint: "" },
  { id: "dsh", label: "DeepSeek Harness", mode: "positional", exists: false, enabled: true,
    ready: false, needs_login: false, command: "dsh", source: "missing",
    aliases: [], version: "", install_cmd: "", notes: "", login_hint: "" },
];

async function fakeFetch(input) {
  const url = String(input);
  let body;
  if (url.includes("/api/health")) {
    body = { build: "r5", routes: 2, features: ["agents", "version", "harness", "dispatch", "install", "install_skill", "locate"] };
  } else if (url.includes("/api/agents")) {
    body = { agents, count: agents.length };
  } else if (url.includes("/api/harness")) {
    body = { harness: [{ id: "claude", label: "Claude", kind: "cli", form: "cli", dir: "C:/x/.claude/skills",
      exists: true, installed: true, mcp_kind: "cli", install_cmd: "", url: "" }] };
  } else if (url.includes("/api/audit")) {
    body = { records: [{ agent: "gemini", ok: true, exit_code: 0, duration_ms: 1300, ts: "2026-10-30T22:00:00", task: "x" }] };
  } else if (url.includes("/api/version")) {
    body = { agent: "gemini", version: "0.65.0" };
  } else {
    body = { ok: true };
  }
  return { status: 200, headers: { get: () => "application/json" }, json: async () => body, text: async () => JSON.stringify(body) };
}

const sandbox = {
  console,
  document,
  fetch: fakeFetch,
  setTimeout: (fn) => setTimeout(fn, 0),
  clearTimeout,
  setInterval: () => 0,
  clearInterval: () => {},
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  navigator: { clipboard: { writeText: async () => {} } },
  addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
  AbortController: class { constructor() { this.signal = null; } abort() {} },
  Promise, JSON, Math, Number, String, Array, Object, Set, Map, Date, RegExp, Error,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

const problems = [];
try {
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: "index.html<script>" });
} catch (err) {
  problems.push(`脚本执行抛错：${err.message}`);
}

setTimeout(() => {
  const body = document.getElementById("body").innerHTML || "";
  if (!body.includes("<tr")) problems.push("表格没渲染出任何行（#body 为空）");
  if (!body.includes("data-probe") && !body.includes("data-locate")) {
    problems.push("版本列的按钮没生成（data-probe / data-locate 都缺）");
  }
  const isEmpty = document.getElementById("empty").classList.contains("hidden");
  if (!isEmpty) problems.push("表格有数据但空态没被隐藏");
  if (!(document.getElementById("s-total").textContent === 2 || document.getElementById("s-total").textContent === "2")) {
    problems.push(`顶栏计数没更新：s-total=${document.getElementById("s-total").textContent}`);
  }

  if (problems.length) {
    for (const p of problems) console.error(`✖ ${p}`);
    process.exit(1);
  }
  console.log(`✔ 脚本执行通过 · 渲染 ${(body.match(/<tr/g) || []).length} 行 · 按钮齐全`);
}, 60);
