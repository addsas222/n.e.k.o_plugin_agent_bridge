// agent_bridge 的 VS Code 扩展。
//
// 设计原则：**装完就能用，坏了能自己说清楚**。
//   - 激活失败不再静默：activate 整体 try/catch，异常直接弹窗 + 进输出面板；
//   - 每个命令都能单独触发激活（activationEvents 显式列了 onCommand:*），
//     不依赖启动时机；
//   - 第一条命令就是「诊断」：逐项告诉你缺什么、点哪个按钮修。
//
// 三层能力（VS Code 官方 Agent 扩展面）：
//   1. contributes.languageModelTools + vscode.lm.registerTool  → 模型自动调用
//   2. contributes.mcpServerDefinitionProviders + registerMcpServerDefinitionProvider → 自带 MCP 服务端
//   3. contributes.commands → 人肉一键派发
"use strict";

const vscode = require("vscode");
const http = require("http");
const fs = require("fs");
const path = require("path");
const { execFile } = require("child_process");
const { URL } = require("url");

const MCP_PROVIDER_ID = "agentBridgeProvider";
const TOOL_DISPATCH = "agent-bridge_vscode_dispatch";
const TOOL_AGENTS = "agent-bridge_vscode_agents";

let channel;
let statusBar;
let busy = false;
let mcpRegistered = false;

const cfg = () => vscode.workspace.getConfiguration("agentBridge");
const baseUrl = () => cfg().get("baseUrl", "http://127.0.0.1:8787");

// ------------------------------------------------------------------ 基础

function request(method, apiPath, payload, timeoutMs = 8000) {
  return new Promise((resolve, reject) => {
    const url = new URL(baseUrl() + apiPath);
    const data = payload === undefined ? null : Buffer.from(JSON.stringify(payload), "utf8");
    const req = http.request(
      {
        hostname: url.hostname,
        port: url.port || 80,
        path: url.pathname + url.search,
        method,
        headers: data ? { "Content-Type": "application/json", "Content-Length": data.length } : {},
      },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf8");
          let body;
          try {
            body = JSON.parse(text);
          } catch {
            body = { error: text.slice(0, 400) };
          }
          resolve({ status: res.statusCode, body });
        });
      }
    );
    req.setTimeout(timeoutMs, () => req.destroy(new Error(`请求超时（${timeoutMs}ms）`)));
    req.on("error", (err) => reject(new Error(err.code === "ECONNREFUSED" ? "没连上（服务没启动）" : err.message)));
    if (data) req.write(data);
    req.end();
  });
}

const fetchAgents = async () => (await request("GET", "/api/agents", undefined, 5000)).body.agents || [];

function repoRoot() {
  const configured = cfg().get("repoRoot", "");
  const has = (dir) => dir && fs.existsSync(path.join(dir, "skill", "neko-bridge", "bin", "neko.py"));
  if (has(configured)) return configured;
  for (const folder of vscode.workspace.workspaceFolders || []) {
    if (folder.uri.scheme !== "file") continue;
    let dir = folder.uri.fsPath;
    for (let i = 0; i < 4 && dir; i += 1) {
      if (has(dir)) return dir;
      const parent = path.dirname(dir);
      if (parent === dir) break;
      dir = parent;
    }
  }
  return "";
}

function pythonExe() {
  return process.platform === "win32" ? "python" : "python3";
}

function probePython() {
  return new Promise((resolve) => {
    execFile(pythonExe(), ["--version"], { timeout: 5000 }, (err, stdout) => {
      resolve(err ? "" : String(stdout || "").trim());
    });
  });
}

function paintStatus(text, tooltip) {
  statusBar.text = text;
  statusBar.tooltip = tooltip;
}

async function refreshStatus() {
  try {
    const agents = await fetchAgents();
    const ready = agents.filter((a) => a.ready).length;
    paintStatus(
      `$(hubot) ${ready}/${agents.length} 可用`,
      `agent_bridge：${ready}/${agents.length} 可用\n点这里看全部（或 F1 →「Agent Bridge: ③」）`
    );
    return agents;
  } catch {
    paintStatus("$(warning) 控制台未启动 · 点我修", "点一下：诊断并启动本地控制台");
    return null;
  }
}

// ------------------------------------------------------------------ 诊断

async function doctor() {
  channel.show(true);
  channel.appendLine("\n=== Agent Bridge 诊断 ===");
  const lines = [];
  const step = (ok, title, detail, fix) => {
    const mark = ok ? "✔" : "✖";
    lines.push(`${mark} ${title}`);
    if (detail) lines.push(`    ${detail}`);
    if (!ok && fix) lines.push(`    → ${fix}`);
  };

  const root = repoRoot();
  step(!!root, "找到 agent_bridge 仓库",
    root || "没找到含 skill/neko-bridge/bin/neko.py 的目录",
    "设置 agentBridge.repoRoot，或用 VS Code 打开该仓库目录");

  const py = await probePython();
  step(!!py, "Python 可用", py || `${pythonExe()} 未找到`,
    "装 Python 3.11+ 并确保它在 PATH 上（控制台与 MCP 都靠它）");

  let agents = null;
  try {
    const res = await request("GET", "/api/agents", undefined, 4000);
    agents = res.body.agents || [];
    step(true, "控制台可达", `${baseUrl()} · ${agents.length} 条路由`);
  } catch (err) {
    step(false, "控制台可达", `${baseUrl()} · ${err.message}`,
      "点下面的「启动控制台」，或手动跑：python webui/server.py");
  }

  if (agents) {
    const ready = agents.filter((a) => a.ready).length;
    const login = agents.filter((a) => a.needs_login).map((a) => a.id);
    step(ready > 0, "有可用的 Agent",
      `${ready}/${agents.length} 就绪` + (login.length ? `；需登录：${login.join("、")}` : ""),
      "按各 CLI 的提示登录后重启控制台");
  }

  step(mcpRegistered, "MCP 已注册给 VS Code",
    mcpRegistered ? MCP_PROVIDER_ID : "未注册（agentBridge.registerMcp=false 或版本不支持）",
    "在设置里把 agentBridge.registerMcp 打开，并升级 VS Code");

  channel.append(lines.join("\n"));
  channel.appendLine("=== 诊断结束 ===");

  const summary = lines.filter((l) => l.startsWith("✔") || l.startsWith("✖")).map((l) => l).join("\n");
  const actions = [];
  if (!agents) actions.push("启动控制台");
  actions.push("看全部状态");
  const picked = await vscode.window.showInformationMessage(
    summary,
    { modal: false, detail: "详细输出在「输出 → Agent Bridge」面板。" },
    ...actions
  );
  if (picked === "启动控制台") await startServer();
  if (picked === "看全部状态") await showStatus();
}

async function startServer() {
  const root = repoRoot();
  if (!root) {
    const picked = await vscode.window.showErrorMessage(
      "找不到 agent_bridge 仓库目录，无法启动控制台。",
      "去设置里指定"
    );
    if (picked) vscode.commands.executeCommand("workbench.action.openSettings", "agentBridge.repoRoot");
    return;
  }
  const terminal = vscode.window.createTerminal({ name: "agent_bridge 控制台", cwd: root });
  terminal.show();
  terminal.sendText(`${pythonExe()} webui/server.py`);
  vscode.window.showInformationMessage(
    `已在终端启动控制台。等它打印出地址后，点状态栏或重跑「诊断」。`
  );
}

// ------------------------------------------------------------------ 派发

async function runDispatch(agent, task, cwd, timeoutSeconds) {
  const timeout = Number(timeoutSeconds) || cfg().get("timeoutSeconds", 300);
  const started = Date.now();
  const { body } = await request(
    "POST",
    "/api/dispatch",
    { agent, task, cwd: cwd || "", timeout_seconds: timeout },
    (timeout + 30) * 1000
  );
  return { ...body, seconds: ((Date.now() - started) / 1000).toFixed(1) };
}

function formatResult(result, agent) {
  const head = result.ok
    ? `✔ ${result.agent || agent} 完成 · ${result.seconds}s`
    : `✖ ${result.agent || agent} 失败 · ${result.seconds}s · exit=${result.exit_code ?? "–"}`;
  const parts = [head];
  if (result.error) parts.push(`! ${result.error}`);
  if (result.output) parts.push(result.output);
  if (result.stderr) parts.push(`--- stderr ---\n${result.stderr}`);
  return parts.join("\n");
}

const STATUS_TEXT = { ready: "可派发", login: "需登录（派了也是白派）", off: "已禁用", missing: "没装" };

function agentState(a) {
  if (!a.exists) return "missing";
  if (!a.enabled) return "off";
  if (a.needs_login) return "login";
  return a.ready ? "ready" : "off";
}

async function dispatchCommand() {
  if (busy) {
    vscode.window.showWarningMessage("上一次派发还没结束，等它跑完。");
    return;
  }
  let agents;
  try {
    agents = await fetchAgents();
  } catch {
    const picked = await vscode.window.showErrorMessage(
      "连不上本地控制台，没法派发。",
      "启动控制台",
      "诊断"
    );
    if (picked === "启动控制台") await startServer();
    if (picked === "诊断") await doctor();
    return;
  }

  const preferred = cfg().get("defaultAgent", "");
  let agent = preferred && agents.some((a) => a.id === preferred && a.ready) ? preferred : "";
  if (!agent) {
    const items = agents
      .slice()
      .sort((a, b) => Number(b.ready) - Number(a.ready))
      .map((a) => ({
        label: `${a.ready ? "$(check)" : "$(circle-slash)"} ${a.id}`,
        description: STATUS_TEXT[agentState(a)],
        detail: a.needs_login ? `需登录：${a.login_hint || "先登录再重启控制台"}` : a.command,
        id: a.id,
        disabled: !a.ready,
      }));
    const picked = await vscode.window.showQuickPick(items, {
      title: "第 1 步 / 共 2 步：派给哪个 Agent",
      placeHolder: "只能选「可派发」的；灰掉的是没装/没登录/已禁用",
      matchOnDescription: true,
    });
    if (!picked) return;
    agent = picked.id;
  }

  const task = await vscode.window.showInputBox({
    title: `第 2 步 / 共 2 步：给 ${agent} 什么任务`,
    prompt: "写清楚要它做什么（它看不到你的对话上下文）。回车开始，Esc 取消。",
    placeHolder: "例：把 README 里的拼写错误列出来，只报告不改文件",
    ignoreFocusOut: true,
  });
  if (!task) return;

  busy = true;
  paintStatus(`$(sync~spin) ${agent} 运行中`, "派发中…");
  channel.show(true);
  try {
    const result = await runDispatch(agent, task);
    channel.appendLine(`\n▶ ${formatResult(result, agent)}`);
    if (result.ok) {
      vscode.window.showInformationMessage(`${agent} 完成（${result.seconds}s），输出在「输出 → Agent Bridge」。`);
    } else if (result.needs_login) {
      vscode.window.showWarningMessage(`${agent} 没登录，任务没执行。换一个 Agent 或先登录。`);
    } else {
      vscode.window.showWarningMessage(`${agent} 失败：${(result.error || "").slice(0, 120)}`);
    }
  } catch (err) {
    channel.appendLine(`✖ ${err.message}`);
    vscode.window.showErrorMessage(String(err.message));
  } finally {
    busy = false;
    refreshStatus();
  }
}

async function showStatus() {
  let agents;
  try {
    agents = await fetchAgents();
  } catch {
    await doctor();
    return;
  }
  channel.show(true);
  channel.appendLine(`\n本机 ${agents.length} 条 Agent 路由 · 控制台 ${baseUrl()}`);
  channel.append(
    agents
      .map((a) => {
        const st = agentState(a);
        const mark = st === "ready" ? "✔" : st === "missing" ? "✖" : "!";
        return `${mark} ${a.id.padEnd(14)} ${STATUS_TEXT[st].padEnd(18)} ${a.command}`;
      })
      .join("\n")
  );
  channel.appendLine("\n✔=可派发  !=需登录/已禁用  ✖=没装");
}

async function setDefaultAgent() {
  try {
    const agents = await fetchAgents();
    const picked = await vscode.window.showQuickPick(
      [{ label: "（每次让我选）", value: "" }, ...agents.map((a) => ({ label: a.id, value: a.id, description: STATUS_TEXT[agentState(a)] }))],
      { title: "默认派给谁" }
    );
    if (!picked) return;
    await cfg().update("defaultAgent", picked.value, vscode.ConfigurationTarget.Global);
    vscode.window.showInformationMessage(`默认 Agent：${picked.value || "每次询问"}`);
  } catch {
    vscode.window.showErrorMessage("连不上控制台，先点「诊断」看看。");
  }
}

// ------------------------------------------------------------------ LM Tools

class DispatchTool {
  async prepareInvocation(options) {
    const agent = options.input.agent || cfg().get("defaultAgent", "") || "（自动挑一个可用的）";
    return {
      invocationMessage: `派发给 ${agent}`,
      confirmationMessages: {
        title: "派发任务给本机 Agent",
        message: new vscode.MarkdownString(
          `将把任务交给 **${agent}**。它会真实运行、可能改文件、可能消耗额度。\n\n> ${options.input.task || ""}`
        ),
      },
    };
  }

  async invoke(options) {
    const input = options.input || {};
    let agent = input.agent || cfg().get("defaultAgent", "");
    if (!agent) {
      const agents = (await fetchAgents()).filter((a) => a.ready);
      if (!agents.length) {
        return new vscode.LanguageModelToolResult([
          new vscode.LanguageModelTextPart("没有可用的 Agent。先在命令面板跑「Agent Bridge: ① 诊断」。"),
        ]);
      }
      agent = agents[0].id;
    }
    const result = await runDispatch(agent, input.task, input.cwd, input.timeoutSeconds);
    const text = formatResult(result, agent);
    channel.appendLine(`\n▶ [模型调用] ${text}`);
    return new vscode.LanguageModelToolResult([new vscode.LanguageModelTextPart(text)]);
  }
}

class AgentsTool {
  async invoke() {
    const agents = await fetchAgents();
    const text =
      `共 ${agents.length} 条，可用 ${agents.filter((a) => a.ready).length}\n` +
      agents.map((a) => `${a.id}\t${STATUS_TEXT[agentState(a)]}`).join("\n");
    return new vscode.LanguageModelToolResult([new vscode.LanguageModelTextPart(text)]);
  }
}

// ------------------------------------------------------------------ MCP

function registerMcp(context) {
  if (!cfg().get("registerMcp", true)) return;
  if (!vscode.lm || typeof vscode.lm.registerMcpServerDefinitionProvider !== "function") return;

  const emitter = new vscode.EventEmitter();
  context.subscriptions.push(emitter);
  context.subscriptions.push(
    vscode.lm.registerMcpServerDefinitionProvider(MCP_PROVIDER_ID, {
      onDidChangeMcpServerDefinitions: emitter.event,
      provideMcpServerDefinitions: async () => {
        const root = repoRoot();
        if (!root) return [];
        return [
          new vscode.McpStdioServerDefinition({
            label: "agent_bridge",
            command: pythonExe(),
            args: [path.join(root, "skill", "neko-bridge", "bin", "neko.py"), "mcp"],
            cwd: vscode.Uri.file(root),
            env: { NEKO_BRIDGE_PROVIDERS: path.join(root, "providers") },
            version: "0.3.0",
          }),
        ];
      },
      resolveMcpServerDefinition: async (server) => server,
    })
  );
  mcpRegistered = true;
}

// ------------------------------------------------------------------ 生命周期

function activate(context) {
  channel = vscode.window.createOutputChannel("Agent Bridge");
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBar.command = "agentBridge.doctor";
  statusBar.text = "$(hubot) Agent Bridge";
  statusBar.tooltip = "点一下：诊断";
  statusBar.show();
  context.subscriptions.push(channel, statusBar);

  try {
    context.subscriptions.push(
      vscode.commands.registerCommand("agentBridge.doctor", doctor),
      vscode.commands.registerCommand("agentBridge.dispatch", dispatchCommand),
      vscode.commands.registerCommand("agentBridge.status", showStatus),
      vscode.commands.registerCommand("agentBridge.pickAgent", setDefaultAgent),
      vscode.commands.registerCommand("agentBridge.startServer", startServer),
      vscode.workspace.onDidChangeConfiguration((e) => {
        if (e.affectsConfiguration("agentBridge.baseUrl")) refreshStatus();
      })
    );

    // LM Tools 是较新的 API：没有就降级，别让整条 activate 挂掉。
    if (vscode.lm && typeof vscode.lm.registerTool === "function") {
      context.subscriptions.push(
        vscode.lm.registerTool(TOOL_DISPATCH, new DispatchTool()),
        vscode.lm.registerTool(TOOL_AGENTS, new AgentsTool())
      );
    } else {
      channel.appendLine("当前 VS Code 不支持 languageModelTools，已降级为「命令 + MCP」模式。");
    }

    registerMcp(context);
    channel.appendLine(`Agent Bridge 已激活 · 控制台 ${baseUrl()}`);
    refreshStatus().then((agents) => {
      // 首次上手：控制台没起来就直接给一条可点的提示，而不是让用户猜
      if (!agents) {
        vscode.window
          .showWarningMessage(
            "Agent Bridge 已装好，但本地控制台没启动。要现在启动吗？",
            "启动控制台",
            "先不用"
          )
          .then((picked) => {
            if (picked === "启动控制台") startServer();
          });
      }
    });
  } catch (err) {
    // 激活失败必须说出来：否则表现就是「命令都在，点了没反应」。
    channel.appendLine(`✖ 激活失败：${err && err.stack ? err.stack : err}`);
    vscode.window
      .showErrorMessage(`Agent Bridge 激活失败：${err && err.message ? err.message : err}`, "看输出")
      .then(() => channel.show(true));
  }
}

function deactivate() {}

module.exports = { activate, deactivate, TOOL_DISPATCH, TOOL_AGENTS, MCP_PROVIDER_ID };
