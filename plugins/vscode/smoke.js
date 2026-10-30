// 冒烟：用桩 vscode 跑一遍 activate，核对**清单声明 == 代码注册**。
//
// 这三处最容易静默错位，且打包/安装/F5 都不会报：
//   1. contributes.commands            ↔ commands.registerCommand
//   2. contributes.languageModelTools  ↔ lm.registerTool（名字必须逐字相同）
//   3. contributes.mcpServerDefinitionProviders ↔ lm.registerMcpServerDefinitionProvider
//
//     node plugins/vscode/smoke.js
"use strict";

const path = require("path");
const Module = require("module");

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const commands = [];
const tools = [];
const providers = [];
const disposables = [];

class EventEmitter {
  constructor() {
    this.event = () => ({ dispose() {} });
  }
  dispose() {}
}

class McpStdioServerDefinition {
  constructor(options) {
    Object.assign(this, options);
  }
}

const stub = {
  window: {
    createOutputChannel: () => ({ append() {}, appendLine() {}, show() {}, dispose() {} }),
    createStatusBarItem: () => ({ show() {}, dispose() {} }),
    // 真实 VS Code 返回 Thenable；activate 里有 .then()，桩必须也是 thenable
    showErrorMessage: (m) => (console.log("  [error]", m), Promise.resolve(undefined)),
    showWarningMessage: (m) => (console.log("  [warn]", m), Promise.resolve(undefined)),
    showInformationMessage: (m) => (console.log("  [info]", m), Promise.resolve(undefined)),
    showQuickPick: async () => undefined,
    showInputBox: async () => undefined,
  },
  commands: {
    registerCommand: (id, fn) => {
      if (typeof fn !== "function") throw new Error(`命令 ${id} 没有处理函数`);
      commands.push(id);
      return { dispose() {} };
    },
  },
  lm: {
    registerTool: (name, tool) => {
      if (!tool || typeof tool.invoke !== "function") throw new Error(`工具 ${name} 没有 invoke`);
      tools.push(name);
      return { dispose() {} };
    },
    registerMcpServerDefinitionProvider: (id, provider) => {
      for (const key of ["provideMcpServerDefinitions", "resolveMcpServerDefinition"]) {
        if (typeof provider[key] !== "function") throw new Error(`provider ${id} 缺 ${key}`);
      }
      providers.push({ id, provider });
      return { dispose() {} };
    },
  },
  workspace: {
    getConfiguration: () => ({ get: (_k, d) => d, update: async () => {} }),
    onDidChangeConfiguration: () => ({ dispose() {} }),
    // 指到真实仓库根，让 repoRoot() 能找到 skill/neko-bridge/bin/neko.py
    workspaceFolders: [{ uri: { scheme: "file", fsPath: REPO_ROOT } }],
  },
  Uri: { file: (p) => ({ fsPath: p }) },
  EventEmitter,
  McpStdioServerDefinition,
  LanguageModelToolResult: class {
    constructor(parts) {
      this.parts = parts;
    }
  },
  LanguageModelTextPart: class {
    constructor(text) {
      this.text = text;
    }
  },
  MarkdownString: class {
    constructor(text) {
      this.value = text;
    }
  },
  StatusBarAlignment: { Left: 1 },
  ConfigurationTarget: { Global: 1 },
};

const originalResolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  if (request === "vscode") return "vscode";
  return originalResolve.call(this, request, ...rest);
};
require.cache.vscode = { id: "vscode", filename: "vscode", loaded: true, exports: stub };

const manifest = require(path.join(__dirname, "package.json"));
const extension = require(path.join(__dirname, "extension.js"));

try {
  extension.activate({ subscriptions: { push: (...items) => disposables.push(...items) } });
} catch (err) {
  console.error(`✖ activate 抛异常（这会让命令全部消失）：${err.message}`);
  process.exit(1);
}

const problems = [];
const declaredCommands = (manifest.contributes.commands || []).map((c) => c.command).sort();
const declaredTools = (manifest.contributes.languageModelTools || []).map((t) => t.name).sort();
const declaredProviders = (manifest.contributes.mcpServerDefinitionProviders || []).map((p) => p.id).sort();

for (const id of declaredCommands) if (!commands.includes(id)) problems.push(`命令未注册：${id}`);
for (const name of declaredTools) if (!tools.includes(name)) problems.push(`LM Tool 未注册：${name}`);
for (const id of declaredProviders) if (!providers.some((p) => p.id === id)) problems.push(`MCP provider 未注册：${id}`);
if (typeof manifest.main !== "string") problems.push("package.json 缺 main");
if (!(manifest.activationEvents || []).length) problems.push("package.json 缺 activationEvents");

// MCP provider 的实际输出必须指向真实文件，否则装上也起不来
(async () => {
  const entry = providers.find((p) => p.id === "agentBridgeProvider");
  if (entry) {
    const defs = await entry.provider.provideMcpServerDefinitions();
    if (defs.length !== 1) problems.push(`MCP 定义数为 ${defs.length}，期望 1`);
    else if (!defs[0].args.join(" ").includes("neko.py")) problems.push("MCP 定义没指向 neko.py");
    else console.log(`  MCP: ${defs[0].command} ${defs[0].args.join(" ")}`);
  }

  console.log(`commands: ${commands.join(", ")}`);
  console.log(`tools:    ${tools.join(", ")}`);
  console.log(`providers:${providers.map((p) => p.id).join(", ")}`);
  if (problems.length) {
    for (const p of problems) console.error(`✖ ${p}`);
    process.exit(1);
  }
  console.log(`✔ 3 层（commands / LM tools / MCP provider）声明与注册一致，disposables=${disposables.length}`);
})();
