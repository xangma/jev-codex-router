#!/usr/bin/env node
/** Opt-in, reversible bridge for the installed native Antigravity desktop app. */
import { spawn } from 'node:child_process';
import { readFile, mkdir, writeFile, chmod, rename } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { installClefRouter } from './antigravity_inject.mjs';

const MODES = new Set(['auto-cheap', 'auto', 'auto-deep']);
const BINDING = '__clefRouteRequest', STATUS_BINDING = '__clefRouteStatus', RESPONSE = '__clefRouteRespond';
const HERE = dirname(fileURLToPath(import.meta.url));
const ID = /^[a-zA-Z0-9-]{8,96}$/;

export function validateDecisionRequest(value) {
  if (!value || !ID.test(value.id || '')) throw new Error('Invalid route request');
  if (value.cancel === true) return { id: value.id, cancel: true };
  if (!MODES.has(value.mode) || typeof value.task !== 'string' || !value.task.trim() || value.task.length > 5100
    || !Array.isArray(value.candidates) || value.candidates.length < 1 || value.candidates.length > 80) throw new Error('Invalid route request');
  const candidates = value.candidates.map(x => {
    if (!x || typeof x.model !== 'string' || !/^MODEL_[A-Z0-9_]{1,150}$/.test(x.model)
      || ![null, 'low', 'medium', 'high', 'xhigh', 'max'].includes(x.effort)
      || typeof x.description !== 'string' || x.description.length > 450) throw new Error('Invalid native inventory');
    return { model: x.model, effort: x.effort, description: x.description };
  });
  const rawContext = value.context;
  let context;
  if (rawContext !== undefined) {
    if (!rawContext || typeof rawContext !== 'object' || Array.isArray(rawContext) || JSON.stringify(rawContext).length > 6000) throw new Error('Invalid route context');
    context = {};
    for (const field of ['images', 'media']) if (Number.isInteger(rawContext[field]) && rawContext[field] >= 0 && rawContext[field] <= 10000) context[field] = rawContext[field];
    if (typeof rawContext.previous_task === 'string') context.previous_task = rawContext.previous_task.slice(0, 2000);
    if (Array.isArray(rawContext.recent_turns)) context.recent_turns = rawContext.recent_turns.slice(-4)
      .filter(x => x && ['user', 'assistant'].includes(x.role) && typeof x.text === 'string')
      .map(x => ({ role: x.role, text: x.text.slice(0, 700) }));
    if (/^MODEL_[A-Z0-9_]{1,150}$/.test(rawContext.previous_route?.model || '')) context.previous_route = {
      model: rawContext.previous_route.model, effort: [null, 'low', 'medium', 'high', 'xhigh', 'max'].includes(rawContext.previous_route.effort) ? rawContext.previous_route.effort : null };
  }
  return { id: value.id, task: value.task, mode: value.mode, candidates, ...(context === undefined ? {} : { context }) };
}

export function invokeDecider(request, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(options.python || 'python3', [options.deciderPath || join(HERE, 'clef_route.py'), '--json'],
      { stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true });
    let output = '', done = false;
    const finish = (error, value) => {
      if (done) return; done = true; clearTimeout(timer); options.signal?.removeEventListener('abort', abort);
      if (error) { child.kill('SIGTERM'); reject(new Error('Local routing unavailable')); } else resolve(value);
    };
    const abort = () => finish(true);
    const timer = setTimeout(abort, Math.max(10, Math.min(24000, options.timeoutMs || 18000)));
    options.signal?.addEventListener('abort', abort, { once: true });
    if (options.signal?.aborted) { abort(); return; }
    child.on('error', abort); child.stdin.on('error', abort);
    child.stdout.on('data', bytes => { output += bytes.toString(); if (output.length > 65536) abort(); });
    child.on('close', code => {
      if (done) return;
      try {
        const result = JSON.parse(output);
        if (code !== 0 || result.source !== 'clef' || !request.candidates.some(x => x.model === result.model && x.effort === (result.effort ?? null))) throw new Error();
        finish(null, { model: result.model, effort: result.effort ?? null, source: 'clef',
          decision_cache: result.decision_cache ? { hit: result.decision_cache.hit === true } : null });
      } catch { abort(); }
    });
    const { id, ...input } = request;
    child.stdin.end(JSON.stringify(input));
  });
}

export class CDPClient {
  constructor(socket) {
    this.socket = socket; this.nextId = 0; this.pending = new Map(); this.listeners = new Set();
    socket.addEventListener('message', event => {
      let message; try { message = JSON.parse(event.data); } catch { return; }
      if (message.id && this.pending.has(message.id)) {
        const pending = this.pending.get(message.id); this.pending.delete(message.id); clearTimeout(pending.timer);
        message.error ? pending.reject(new Error('Native control request failed')) : pending.resolve(message.result);
      } else if (message.method) for (const listener of this.listeners) listener(message);
    });
    socket.addEventListener('close', () => {
      for (const pending of this.pending.values()) { clearTimeout(pending.timer); pending.reject(new Error('Native control disconnected')); }
      this.pending.clear(); this.onClose?.();
    });
  }
  static async connect(url) {
    const socket = new WebSocket(url);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { socket.close(); reject(new Error('Native control unavailable')); }, 5000);
      socket.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
      socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('Native control unavailable')); }, { once: true });
    });
    return new CDPClient(socket);
  }
  call(method, params = {}, timeoutMs = 10000) {
    if (this.pending.size > 64) return Promise.reject(new Error('Native control busy'));
    return new Promise((resolve, reject) => {
      const id = ++this.nextId;
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('Native control timed out')); }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      try { this.socket.send(JSON.stringify({ id, method, params })); } catch { clearTimeout(timer); this.pending.delete(id); reject(new Error('Native control disconnected')); }
    });
  }
  async evaluate(expression, contextId) {
    const result = await this.call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true,
      ...(contextId === undefined ? {} : { contextId }) });
    if (result.exceptionDetails) throw new Error('Native bridge evaluation failed');
    return result.result?.value;
  }
  close() { this.socket.close(); }
}

async function discoverTarget(port) {
  const ports = [];
  if (port !== 'auto') ports.push(Number(port));
  else {
    try {
      const text = await readFile(join(homedir(), 'Library/Application Support/Antigravity/DevToolsActivePort'), 'utf8');
      const detected = Number(text.split('\n', 1)[0]); if (Number.isInteger(detected) && detected > 0 && detected < 65536) ports.push(detected);
    } catch { /* Existing known app endpoint is also supported. */ }
    ports.push(57798);
  }
  for (const candidate of [...new Set(ports)]) {
    if (!Number.isInteger(candidate) || candidate < 1 || candidate > 65535) continue;
    try {
      const response = await fetch(`http://127.0.0.1:${candidate}/json/list`, { signal: AbortSignal.timeout(3000) });
      const targets = await response.json();
      const target = targets.find(x => x.type === 'page' && ['127.0.0.1', 'localhost', '[::1]'].includes(new URL(x.url).hostname));
      if (!target) continue;
      const endpoint = new URL(target.webSocketDebuggerUrl);
      if (endpoint.protocol === 'ws:' && ['127.0.0.1', 'localhost', '[::1]'].includes(endpoint.hostname) && Number(endpoint.port) === candidate) return endpoint.href;
    } catch { /* Never report debugger URLs or native page details. */ }
  }
  throw new Error('Open Antigravity before starting Auto');
}

function parseArgs(argv) {
  const options = { mode: 'auto', port: 'auto', python: 'python3', timeoutMs: 20000,
    stateDir: join(homedir(), '.local/share/clef/antigravity-auto') };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (['--status', '--verify', '--stop', '--help'].includes(arg)) options.action = arg.slice(2);
    else if (['--mode', '--port', '--python', '--timeout-ms', '--state-dir'].includes(arg) && argv[i + 1]) {
      const name = { '--timeout-ms': 'timeoutMs', '--state-dir': 'stateDir' }[arg] || arg.slice(2);
      options[name] = argv[++i];
    } else throw new Error('Unknown bridge option');
  }
  options.timeoutMs = Number(options.timeoutMs);
  if (!MODES.has(options.mode) || !Number.isInteger(options.timeoutMs) || options.timeoutMs < 1000 || options.timeoutMs > 25000) throw new Error('Invalid bridge mode or timeout');
  if (options.port !== 'auto' && (!/^\d+$/.test(options.port) || Number(options.port) < 1 || Number(options.port) > 65535)) throw new Error('Invalid native control port');
  return options;
}

export async function main(argv = process.argv.slice(2)) {
  const options = parseArgs(argv);
  if (options.action === 'help') {
    console.log('antigravity-clef [--mode auto-cheap|auto|auto-deep] [--port auto|PORT] [--python PATH] [--timeout-ms 1000..25000] [--state-dir PATH]\nUse --status, --verify or --stop for the existing native bridge. Ctrl-C removes this bridge.'); return;
  }
  const client = await CDPClient.connect(await discoverTarget(options.port));
  if (options.action) {
    try {
      if (options.action === 'status') console.log(JSON.stringify(await client.evaluate('globalThis.__clefAntigravityAuto?.getStatus() ?? {enabled:false}')));
      else if (options.action === 'verify') console.log(JSON.stringify(await client.evaluate('globalThis.__clefAntigravityAuto?.verifyLastRoute() ?? {verified:false,reason:"bridge-disabled"}')));
      else {
        await client.evaluate('globalThis.__clefAntigravityAuto?.requestStop(); true');
        for (let attempt = 0; attempt < 30; attempt++) {
          const complete = await client.evaluate('!globalThis.__clefAntigravityAuto || globalThis.__clefAntigravityAuto.controllerStopped === true');
          if (complete) break;
          if (attempt === 29) throw new Error('Native bridge stop pending');
          await new Promise(resolve => setTimeout(resolve, 100));
        }
      }
    } finally { client.close(); }
    return;
  }
  let scriptId, stopped = false, installed = false, writeChain = Promise.resolve();
  const active = new Map(), metadata = { enabled: true, mode: options.mode, pid: process.pid, records: [] };
  const writeStatus = () => {
    const snapshot = JSON.stringify(metadata);
    writeChain = writeChain.catch(() => {}).then(async () => {
      await mkdir(options.stateDir, { recursive: true, mode: 0o700 }); await chmod(options.stateDir, 0o700);
      const temporary = join(options.stateDir, `status.${process.pid}.tmp`), file = join(options.stateDir, 'status.json');
      await writeFile(temporary, snapshot + '\n', { mode: 0o600 }); await chmod(temporary, 0o600); await rename(temporary, file);
    }).catch(() => { /* Metadata storage failure cannot block native turns. */ });
  };
  const cleanup = async () => {
    if (stopped) return; stopped = true;
    for (const abort of active.values()) abort.abort(); active.clear();
    if (installed) {
      try { await client.evaluate('globalThis.__clefAntigravityAuto?.uninstall(); true'); } catch { /* App may have exited. */ }
      if (scriptId) try { await client.call('Page.removeScriptToEvaluateOnNewDocument', { identifier: scriptId }); } catch { /* Disconnected. */ }
      for (const name of [BINDING, STATUS_BINDING]) try { await client.call('Runtime.removeBinding', { name }); } catch { /* Disconnected. */ }
      try { await client.evaluate('if(globalThis.__clefAntigravityAuto) globalThis.__clefAntigravityAuto.controllerStopped=true; true'); } catch { /* Disconnected. */ }
    }
    if (installed) { metadata.enabled = false; writeStatus(); await writeChain; }
    client.close();
  };
  let resolveExit;
  const exited = new Promise(resolve => { resolveExit = resolve; });
  const end = () => { cleanup().finally(resolveExit); };
  process.once('SIGINT', end); process.once('SIGTERM', end); client.onClose = end;
  try {
    if (await client.evaluate('globalThis.__clefAntigravityAuto?.enabled === true')) throw new Error('Auto bridge already running');
    await client.call('Runtime.enable'); await client.call('Page.enable');
    for (const name of [BINDING, STATUS_BINDING]) await client.call('Runtime.addBinding', { name });
    installed = true;
    client.listeners.add(message => {
      if (message.method !== 'Runtime.bindingCalled') return;
      const { name, payload, executionContextId } = message.params || {};
      if (typeof payload !== 'string' || payload.length > 128000 || stopped) return;
      if (name === STATUS_BINDING) {
        try {
          const raw = JSON.parse(payload);
          if (raw.action === 'stop') { end(); return; }
          if (!ID.test(raw.id || '') || !['dispatched', 'fallback', 'cancelled', 'skipped'].includes(raw.outcome) || !MODES.has(raw.mode)) return;
          const record = { id: raw.id, at: Number(raw.at), mode: raw.mode, outcome: raw.outcome, source: raw.source === 'clef' ? 'clef' : 'native', ms: Number(raw.ms) };
          if (/^MODEL_[A-Z0-9_]{1,150}$/.test(raw.model || '')) record.model = raw.model;
          if ([null, 'low', 'medium', 'high', 'xhigh', 'max'].includes(raw.effort)) record.effort = raw.effort;
          metadata.mode = raw.mode; metadata.records.push(record); metadata.records = metadata.records.slice(-128); writeStatus();
          if (raw.outcome === 'cancelled') active.get(raw.id)?.abort();
        } catch { /* Invalid status payload is ignored. */ }
        return;
      }
      if (name !== BINDING) return;
      (async () => {
        let request;
        try { request = validateDecisionRequest(JSON.parse(payload)); } catch { return; }
        if (request.cancel) { active.get(request.id)?.abort(); return; }
        let result = { error: 'unavailable' };
        if (active.size < 8 && !active.has(request.id)) {
          const abort = new AbortController(); active.set(request.id, abort);
          try { result = await invokeDecider(request, { ...options, timeoutMs: options.timeoutMs - 1000, signal: abort.signal }); }
          catch { /* Fail open in the renderer with an explicit status. */ }
          finally { active.delete(request.id); }
        }
        if (!stopped) try { await client.evaluate(`globalThis.${RESPONSE}?.(${JSON.stringify(request.id)},${JSON.stringify(result)})`, executionContextId); } catch { /* The conversation may have closed. */ }
      })().catch(() => {});
    });
    const source = `(${installClefRouter.toString()})(${JSON.stringify({ mode: options.mode, timeoutMs: options.timeoutMs,
      bindingName: BINDING, statusBindingName: STATUS_BINDING, responseName: RESPONSE })})`;
    scriptId = (await client.call('Page.addScriptToEvaluateOnNewDocument', { source })).identifier;
    const result = await client.evaluate(source);
    if (!result?.installed) throw new Error('Native renderer unavailable');
    writeStatus(); console.log(JSON.stringify({ enabled: true, mode: options.mode, pid: process.pid, statusFile: join(options.stateDir, 'status.json') }));
    await exited;
  } catch (error) { await cleanup(); throw error; }
  finally { process.removeListener('SIGINT', end); process.removeListener('SIGTERM', end); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(() => { console.error('Antigravity Auto unavailable; native configuration is unchanged.'); process.exitCode = 1; });
}
