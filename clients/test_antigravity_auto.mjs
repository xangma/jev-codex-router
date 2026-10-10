import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { randomUUID } from 'node:crypto';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { installClefRouter } from './antigravity_inject.mjs';
import { validateDecisionRequest, invokeDecider, CDPClient } from './antigravity_auto.mjs';

const SERVICE = '/exa.language_server_pb.LanguageServerService/';
const MODEL = 'MODEL_PLACEHOLDER_M320';
const catalogue = [
  { label: 'Gemini 3.8 Flash (Low)', modelId: 'gemini-3.8-flash-low', modelOrAlias: { model: MODEL }, supportsImages: true },
  { label: 'Claude Opus 5.5 (High)', modelOrAlias: { model: 'MODEL_PLACEHOLDER_M402' }, supportsImages: true },
  { label: 'GPT-OSS 120B (Medium)', modelOrAlias: { model: 'MODEL_OPENAI_GPT_OSS_120B_MEDIUM' } },
  { label: 'Disabled (Low)', modelOrAlias: { model: 'MODEL_DISABLED' }, disabled: true },
  { label: 'Unverified alias', modelOrAlias: { alias: 'not-an-enum' } },
];
const response = (value, ok = true) => ({ ok, json: async () => structuredClone(value) });
function element() {
  return { style: {}, children: [], listeners: {}, textContent: '',
    append(...children) { this.children.push(...children); },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    remove() { this.removed = true; } };
}
function environment(t, options = {}) {
  const requests = [], statuses = [], calls = [], elements = [], sockets = [];
  let catalogueCalls = 0;
  class Socket extends EventTarget {
    static OPEN = 1;
    constructor() { super(); this.readyState = 1; this.sent = []; sockets.push(this); }
    send(data) { this.sent.push(data); }
    close() { this.readyState = 3; this.dispatchEvent(new Event('close')); }
  }
  const originalSend = Socket.prototype.send;
  const originalFetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    const body = init?.body ?? (input instanceof Request ? await input.clone().text() : null);
    calls.push({ url, input, init, body });
    if (url.endsWith('GetCascadeModelConfigData')) return response({ clientModelConfigs:
      options.catalogue ? options.catalogue(++catalogueCalls) : catalogue });
    if (url.endsWith('GetCascadeTrajectoryGeneratorMetadata')) return response(options.metadataResponse ? options.metadataResponse() : { generatorMetadata: options.metadata?.() || [] });
    if (url.endsWith('GetCascadeTrajectorySteps')) return response({ steps: options.steps?.() || [{ userInput: { query: 'Prior fixture task' } }] });
    if (options.nativeReject) throw new Error('native transport failed');
    return response({});
  };
  const sandbox = { WebSocket: Socket, fetch: originalFetch, URL, Request, Headers, Uint8Array, ArrayBuffer,
    DataView, TextEncoder, TextDecoder, AbortController, DOMException,
    setTimeout, clearTimeout, crypto: { randomUUID }, location: { origin: 'https://127.0.0.1:1234', href: 'https://127.0.0.1:1234/' },
    __APP_CONFIG__: { csrfToken: 'fixture-native-secret' },
    document: { createElement() { const e = element(); elements.push(e); return e; }, documentElement: options.documentReady === false ? null : element(),
      listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; }, removeEventListener(type, fn) { if (this.listeners[type] === fn) delete this.listeners[type]; } },
    __clefRouteRequest(payload) { const req = JSON.parse(payload); requests.push(req); options.onRequest?.(req, sandbox); },
    __clefRouteStatus(payload) { statuses.push(JSON.parse(payload)); },
  };
  vm.createContext(sandbox);
  vm.runInContext(`(${installClefRouter.toString()})(${JSON.stringify({ timeoutMs: 500, ...options.config })})`, sandbox);
  t.after(() => sandbox.__clefAntigravityAuto.uninstall());
  return { sandbox, Socket, sockets, requests, statuses, calls, elements, originalSend, originalFetch,
    api: sandbox.__clefAntigravityAuto,
    reply(req, result = { model: MODEL, effort: 'low', source: 'clef' }) { return sandbox.__clefRouteRespond(req.id, result); } };
}
const pause = () => new Promise(resolve => setTimeout(resolve, 2));
function grpcJson(payload) {
  const json = new TextEncoder().encode(JSON.stringify(payload)), body = new Uint8Array(json.length + 5);
  body.set(json, 5); new DataView(body.buffer).setUint32(1, json.length);
  return body;
}
function grpcPayload(body) {
  assert.ok(body instanceof Uint8Array);
  assert.equal(body[0], 0);
  assert.equal(new DataView(body.buffer, body.byteOffset, body.byteLength).getUint32(1), body.byteLength - 5);
  return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(body.subarray(5)));
}
async function until(predicate) {
  for (let attempt = 0; attempt < 150; attempt++) { if (predicate()) return; await pause(); }
  assert.fail('fixture did not reach expected state');
}
function frame(overrides = {}) {
  return { streamId: randomUUID(), type: 'start', stream: false, procedure: SERVICE + 'SendUserCascadeMessage',
    headers: { 'x-codeium-csrf-token': ['fixture-native-secret'] },
    payload: { cascadeId: 'fixture-conversation', items: [{ text: 'Private fixture task: reply OK.' }],
      images: [], media: [{ mimeType: 'application/pdf', bytes: 'preserved' }],
      cascadeConfig: { executorConfig: { useCoreDirect: true }, plannerConfig: {
        planModel: 'MODEL_PLACEHOLDER_M402', requestedModel: { alias: 'old-alias' }, modelName: 'stale-model',
        toolConfig: { permissionConfig: { defaultGrants: { deny: ['everything'] } } }, maxOutputTokens: 1234 } },
      customAgentSpec: { builtinAgent: { defaultAgent: { isInteractive: true } } }, tags: ['fixture'] }, ...overrides };
}

test('native catalogue mapping and complete payload preservation', async t => {
  const env = environment(t), socket = new env.Socket(), original = frame();
  socket.send(JSON.stringify(original));
  assert.equal(socket.sent.length, 0);
  await until(() => env.requests.length === 1);
  assert.deepEqual(env.requests[0].candidates.map(x => [x.model, x.effort]), [
    [MODEL, 'low'], ['MODEL_PLACEHOLDER_M402', 'high'], ['MODEL_OPENAI_GPT_OSS_120B_MEDIUM', 'medium']]);
  assert.equal(JSON.stringify(env.requests).includes('fixture-native-secret'), false);
  assert.equal(JSON.stringify(env.requests).includes('fixture-conversation'), false);
  env.reply(env.requests[0]);
  await until(() => socket.sent.length === 1);
  const expected = structuredClone(original);
  expected.payload.cascadeConfig.plannerConfig.planModel = MODEL;
  expected.payload.cascadeConfig.plannerConfig.requestedModel = { model: MODEL };
  delete expected.payload.cascadeConfig.plannerConfig.modelName;
  assert.deepEqual(JSON.parse(socket.sent[0]), expected);
  const status = JSON.stringify(env.api.getStatus());
  assert.equal(status.includes('Private fixture'), false);
  assert.equal(status.includes('fixture-native-secret'), false);
  assert.equal(env.api.getStatus().last.outcome, 'dispatched');
});

test('all three preferences retain the complete available inventory', async t => {
  const env = environment(t), socket = new env.Socket();
  for (const [i, mode] of ['auto-cheap', 'auto', 'auto-deep'].entries()) {
    assert.equal(env.api.setMode(mode), true); socket.send(JSON.stringify(frame()));
    await until(() => env.requests.length === i + 1); env.reply(env.requests[i]);
    await until(() => socket.sent.length === i + 1);
    assert.equal(env.requests[i].mode, mode);
    assert.equal(env.requests[i].candidates.length, 3);
  }
  env.elements[1].listeners.click();
  assert.equal(env.api.getStatus().mode, 'auto-cheap');
});

test('cancelled queued turn never dispatches after a late answer', async t => {
  const env = environment(t), socket = new env.Socket(), original = frame();
  socket.send(JSON.stringify(original)); await until(() => env.requests.length === 1);
  const cancellation = JSON.stringify({ streamId: original.streamId, type: 'cancel' });
  socket.send(cancellation); await until(() => env.statuses.length === 1);
  assert.equal(env.reply(env.requests[0]), false);
  assert.deepEqual(socket.sent, [cancellation]);
  assert.equal(env.statuses[0].outcome, 'cancelled');
  assert.equal(env.requests[1].cancel, true);
});

test('socket closure aborts its classifier without forwarding', async t => {
  const env = environment(t), socket = new env.Socket();
  socket.send(JSON.stringify(frame())); await until(() => env.requests.length === 1);
  socket.close(); await until(() => env.statuses.length === 1);
  assert.equal(env.reply(env.requests[0]), false); assert.equal(socket.sent.length, 0);
});

test('other procedures, streaming calls and invalid frames fall through immediately', t => {
  const env = environment(t), socket = new env.Socket();
  for (const value of ['not-json', JSON.stringify(frame({ procedure: SERVICE + 'StartCascade' })),
    JSON.stringify(frame({ stream: true })), new Uint8Array([1, 2])]) socket.send(value);
  assert.equal(socket.sent.length, 4); assert.equal(env.requests.length, 0);
});

test('custom provider requests preserve their endpoint and native selection', t => {
  const env = environment(t), socket = new env.Socket(), value = frame();
  value.payload.cascadeConfig.plannerConfig.customModelInfoOverride = { modelName: 'private-local-endpoint', apiKey: 'fixture-private-key' };
  const data = JSON.stringify(value); socket.send(data);
  assert.deepEqual(socket.sent, [data]); assert.equal(env.requests.length, 0);
  assert.equal(env.api.getStatus().last.outcome, 'skipped');
  assert.equal(JSON.stringify(env.api.getStatus()).includes('fixture-private-key'), false);
});

for (const kind of ['invalid', 'stale', 'mode-change']) {
  test(`${kind} decision retains the exact original native frame`, async t => {
    const env = environment(t, { catalogue: n => kind === 'stale' && n > 1 ? catalogue.filter(x => x.modelOrAlias?.model !== MODEL) : catalogue });
    const socket = new env.Socket(), data = JSON.stringify(frame()); socket.send(data);
    await until(() => env.requests.length === 1);
    if (kind === 'mode-change') env.api.setMode('auto-deep');
    env.reply(env.requests[0], { source: 'clef', model: kind === 'invalid' ? 'MODEL_UNKNOWN' : MODEL, effort: 'low' });
    await until(() => socket.sent.length === 1);
    assert.deepEqual(socket.sent, [data]); assert.equal(env.api.getStatus().last.outcome, 'fallback');
  });
}

test('classification timeout fails open and rejects the stale callback', async t => {
  const env = environment(t, { config: { timeoutMs: 20 } }), socket = new env.Socket(), data = JSON.stringify(frame());
  socket.send(data); await until(() => env.requests.length === 1); await until(() => socket.sent.length === 1);
  assert.deepEqual(socket.sent, [data]); assert.equal(env.reply(env.requests[0]), false);
  assert.equal(env.api.getStatus().last.reason, 'timeout');
});

test('image requests use only explicitly image-capable native models', async t => {
  const env = environment(t), socket = new env.Socket(), value = frame(); value.payload.images = [{ data: 'retained' }];
  socket.send(JSON.stringify(value)); await until(() => env.requests.length === 1);
  assert.equal(env.requests[0].context.images, 1);
  assert.equal(env.requests[0].candidates.length, 2); env.reply(env.requests[0]); await until(() => socket.sent.length === 1);
});

test('HTTP fallback routes while preserving native headers and attachments', async t => {
  const env = environment(t), native = frame().payload;
  const promise = env.sandbox.fetch('https://127.0.0.1:1234' + SERVICE + 'SendUserCascadeMessage',
    { method: 'POST', headers: { Authorization: 'native-opaque' }, body: JSON.stringify(native) });
  await until(() => env.requests.length === 1); env.reply(env.requests[0]); await promise;
  const sends = env.calls.filter(x => x.url.endsWith('SendUserCascadeMessage'));
  assert.equal(sends.length, 1); assert.equal(sends[0].init.headers.Authorization, 'native-opaque');
  assert.deepEqual(JSON.parse(sends[0].body).media, native.media);
  assert.equal(JSON.parse(sends[0].body).cascadeConfig.plannerConfig.planModel, MODEL);
});

test('native grpc-web JSON bytes route and preserve every unrelated field and fetch option', async t => {
  const env = environment(t), native = frame().payload;
  native.items[0].text = 'Fixture Unicode task: café, 日本語. Reply OK.';
  const original = grpcJson(native), snapshot = original.slice(), abort = new AbortController();
  const headers = new Headers({ 'content-type': 'application/grpc-web+json', 'x-codeium-csrf-token': 'fixture-native-secret', 'x-grpc-web': '1' });
  const init = { method: 'POST', headers, body: original, signal: abort.signal, redirect: 'error', credentials: 'same-origin' };
  const promise = env.sandbox.fetch('https://127.0.0.1:1234' + SERVICE + 'SendUserCascadeMessage', init);
  await until(() => env.requests.length === 1); env.reply(env.requests[0]); await promise;
  const sends = env.calls.filter(x => x.url.endsWith('SendUserCascadeMessage'));
  assert.equal(sends.length, 1);
  assert.equal(sends[0].init.headers, headers); assert.equal(sends[0].init.signal, abort.signal);
  assert.equal(sends[0].init.redirect, 'error'); assert.equal(sends[0].init.credentials, 'same-origin');
  const expected = structuredClone(native);
  expected.cascadeConfig.plannerConfig.planModel = MODEL;
  expected.cascadeConfig.plannerConfig.requestedModel = { model: MODEL };
  delete expected.cascadeConfig.plannerConfig.modelName;
  assert.deepEqual(grpcPayload(sends[0].body), expected);
  assert.deepEqual(original, snapshot); assert.notEqual(sends[0].body, original);
  assert.equal(JSON.stringify(env.requests).includes('fixture-native-secret'), false);
  assert.equal(env.api.getStatus().last.outcome, 'dispatched');
});

test('unsupported and malformed grpc-web bodies retain exact native bytes and options', async t => {
  const env = environment(t), valid = grpcJson(frame().payload);
  const modify = fn => { const body = valid.slice(); fn(body); return body; };
  const multiple = new Uint8Array(valid.length * 2); multiple.set(valid); multiple.set(valid, valid.length);
  const invalidUtf8 = new Uint8Array([0, 0, 0, 0, 2, 0xc0, 0xaf]);
  const cases = [
    [valid, 'application/grpc-web+proto'], [valid, 'application/json'],
    [modify(x => { x[0] = 1; })], [modify(x => { x[0] = 128; })], [modify(x => { x[0] = 2; })],
    [modify(x => { new DataView(x.buffer).setUint32(1, x.length - 4); })],
    [valid.subarray(0, 4)], [multiple], [invalidUtf8], [grpcJson([])],
    [new Uint8Array(16 * 1024 * 1024 + 6)],
    [new TextEncoder().encode(JSON.stringify(frame().payload))],
  ];
  for (const [body, contentType = 'application/grpc-web+json'] of cases) {
    const init = { method: 'POST', headers: { 'content-type': contentType }, body, redirect: 'error' };
    await env.sandbox.fetch('https://127.0.0.1:1234' + SERVICE + 'SendUserCascadeMessage', init);
    assert.equal(env.calls.at(-1).body, body); assert.equal(env.calls.at(-1).init, init);
  }
  assert.equal(env.requests.length, 0); assert.equal(env.statuses.length, 0);
});

test('grpc-web classification fallback preserves the original byte body without rewriting', async t => {
  const env = environment(t), body = grpcJson(frame().payload), snapshot = body.slice();
  const init = { method: 'POST', headers: { 'content-type': 'application/grpc-web+json' }, body };
  const promise = env.sandbox.fetch('https://127.0.0.1:1234' + SERVICE + 'SendUserCascadeMessage', init);
  await until(() => env.requests.length === 1);
  env.reply(env.requests[0], { source: 'clef', model: 'MODEL_UNKNOWN', effort: 'low' }); await promise;
  const sends = env.calls.filter(x => x.url.endsWith('SendUserCascadeMessage'));
  assert.equal(sends.length, 1); assert.equal(sends[0].body, body); assert.equal(sends[0].init, init);
  assert.deepEqual(body, snapshot); assert.equal(env.api.getStatus().last.outcome, 'fallback');
});

test('grpc-web Request bodies with a byte offset retain framing and native options', async t => {
  const env = environment(t), bytes = grpcJson(frame().payload), backing = new Uint8Array(bytes.length + 9);
  backing.set(bytes, 4); const body = backing.subarray(4, bytes.length + 4);
  const request = new Request('https://127.0.0.1:1234' + SERVICE + 'SendUserCascadeMessage', {
    method: 'POST', headers: { 'content-type': 'application/grpc-web+json; charset=utf-8', 'x-native': 'fixture' },
    body, redirect: 'error', credentials: 'same-origin' });
  const promise = env.sandbox.fetch(request);
  await until(() => env.requests.length === 1); env.reply(env.requests[0]); await promise;
  const send = env.calls.find(x => x.url.endsWith('SendUserCascadeMessage'));
  assert.ok(send); assert.equal(send.init, undefined);
  assert.equal(send.url, request.url);
  // The fixture reads Request bodies as text; inspect the dispatched Request
  // independently to verify its binary envelope and options.
  const native = send.input;
  assert.equal(native.headers.get('x-native'), 'fixture'); assert.equal(native.redirect, 'error');
  assert.equal(native.credentials, 'same-origin');
  const changed = grpcPayload(new Uint8Array(await native.clone().arrayBuffer()));
  assert.equal(changed.cascadeConfig.plannerConfig.planModel, MODEL);
});

for (const encoding of ['plain JSON', 'grpc-web JSON']) {
  const requestInit = () => encoding === 'plain JSON'
    ? { method: 'POST', body: JSON.stringify(frame().payload) }
    : { method: 'POST', headers: { 'content-type': 'application/grpc-web+json' }, body: grpcJson(frame().payload) };
  test(`native HTTP ${encoding} rejection never resubmits the user turn`, async t => {
    const env = environment(t, { nativeReject: true });
    const promise = env.sandbox.fetch('https://127.0.0.1:1234' + SERVICE + 'SendUserCascadeMessage', requestInit());
    const failure = assert.rejects(promise, /native transport failed/);
    await until(() => env.requests.length === 1); env.reply(env.requests[0]); await failure;
    assert.equal(env.calls.filter(x => x.url.endsWith('SendUserCascadeMessage')).length, 1);
  });

  test(`HTTP ${encoding} cancellation cannot dispatch a delayed classification result`, async t => {
    const env = environment(t), abort = new AbortController();
    const promise = env.sandbox.fetch('https://127.0.0.1:1234' + SERVICE + 'SendUserCascadeMessage',
      { ...requestInit(), signal: abort.signal });
    const failure = assert.rejects(promise, { name: 'AbortError' });
    await until(() => env.requests.length === 1); abort.abort(); await failure;
    assert.equal(env.reply(env.requests[0]), false);
    assert.equal(env.calls.filter(x => x.url.endsWith('SendUserCascadeMessage')).length, 0);
  });
}

test('verification requires fresh backend metadata and returns no reply text', async t => {
  let metadata = [{ stepIndices: [0], plannerConfig: { planModel: MODEL, modelName: 'old-native-model' }, messages: [{ text: 'private response' }] }];
  const env = environment(t, { metadata: () => metadata }), socket = new env.Socket();
  socket.send(JSON.stringify(frame())); await until(() => env.requests.length === 1); env.reply(env.requests[0]);
  await until(() => socket.sent.length === 1);
  assert.equal((await env.api.verifyLastRoute()).verified, false);
  metadata.push({ chatModel: { model: MODEL, responseModel: 'gemini-3.8-flash-low' }, plannerConfig: { planModel: MODEL, modelName: 'gemini-3.8-flash-low' }, messages: [{ text: 'private response' }] });
  const proof = await env.api.verifyLastRoute();
  assert.equal(proof.verified, true); assert.equal(proof.baselineGeneratorMetadataCount, 1);
  assert.equal(proof.modelName, 'gemini-3.8-flash-low');
  assert.equal(JSON.stringify(proof).includes('private response'), false);
  for (const call of env.calls.filter(x => x.url.endsWith('GetCascadeTrajectoryGeneratorMetadata')))
    assert.equal(JSON.parse(call.body).includeMessages, false);
});

test('contrary actual model identifier cannot verify an echoed selected plan', async t => {
  const metadata = [];
  const env = environment(t, { metadata: () => metadata }), socket = new env.Socket();
  socket.send(JSON.stringify(frame())); await until(() => env.requests.length === 1); env.reply(env.requests[0]);
  await until(() => socket.sent.length === 1);
  metadata.push({ chatModel: { model: MODEL, responseModel: 'different-backend-model' }, plannerConfig: { planModel: MODEL } });
  const proof = await env.api.verifyLastRoute();
  assert.equal(proof.verified, false); assert.equal(proof.responseModel, 'different-backend-model');
});

test('native omitted empty repeated metadata supports fresh-chat routing and baseline proof', async t => {
  let data = {};
  const env = environment(t, { metadataResponse: () => data }), socket = new env.Socket();
  socket.send(JSON.stringify(frame())); await until(() => env.requests.length === 1); env.reply(env.requests[0]);
  await until(() => socket.sent.length === 1);
  assert.equal(env.api.getStatus().last.outcome, 'dispatched');
  assert.equal((await env.api.verifyLastRoute()).verified, false);
  data = { generatorMetadata: [{ chatModel: { model: MODEL, responseModel: 'gemini-3.8-flash' },
    plannerConfig: { planModel: MODEL, modelName: 'gemini-3.8-flash-low' } }] };
  const proof = await env.api.verifyLastRoute();
  assert.equal(proof.verified, true); assert.equal(proof.baselineGeneratorMetadataCount, 0);
});

test('wrong typed metadata and nonempty malformed objects remain failures', async t => {
  for (const data of [null, [], { generatorMetadata: null }, { generatorMetadata: 'invalid' }, { error: 'private fixture error' }, { generatorMetadata: [], error: 'private fixture error' }, { unexpected: true }]) {
    const env = environment(t, { metadataResponse: () => data }), socket = new env.Socket(), original = JSON.stringify(frame());
    socket.send(original); await until(() => socket.sent.length === 1);
    assert.deepEqual(socket.sent, [original]); assert.equal(env.requests.length, 0);
  }
});

test('document-start overlay mounts on DOMContentLoaded and uninstalls cleanly', t => {
  const env = environment(t, { documentReady: false });
  assert.equal(env.elements.length, 0);
  assert.equal(typeof env.sandbox.document.listeners.DOMContentLoaded, 'function');
  env.sandbox.document.documentElement = element(); env.sandbox.document.listeners.DOMContentLoaded();
  assert.equal(env.elements.length, 3); assert.equal(env.elements[1].textContent, 'Auto Standard');
  env.api.uninstall(); assert.equal(env.elements[0].removed, true);
  assert.equal(env.sandbox.document.listeners.DOMContentLoaded, undefined);
});

test('uninstall before DOM creation removes the deferred overlay listener', t => {
  const env = environment(t, { documentReady: false });
  const deferred = env.sandbox.document.listeners.DOMContentLoaded;
  env.api.uninstall(); assert.equal(env.sandbox.document.listeners.DOMContentLoaded, undefined);
  env.sandbox.document.documentElement = element(); deferred(); assert.equal(env.elements.length, 0);
});

test('first attached existing-chat continuation includes bounded prior native context', async t => {
  const env = environment(t, { metadata: () => [{ stepIndices: [50], plannerConfig: { planModel: MODEL } }],
    steps: () => [{ userInput: { query: 'Investigate the failing fixture.' } },
      { plannerResponse: { response: 'I found the issue and can fix the fixture.', thinking: 'do not export reasoning' } }] });
  const socket = new env.Socket(), value = frame(); value.payload.items = [{ text: 'Go ahead.' }];
  socket.send(JSON.stringify(value)); await until(() => env.requests.length === 1);
  assert.deepEqual(env.requests[0].context.recent_turns.map(x => x.role), ['user', 'assistant']);
  assert.equal(env.requests[0].context.recent_turns[0].text, 'Investigate the failing fixture.');
  assert.equal(JSON.stringify(env.requests[0]).includes('do not export reasoning'), false);
  const read = env.calls.find(x => x.url.endsWith('GetCascadeTrajectorySteps'));
  assert.equal(JSON.parse(read.body).stepOffset, 38); env.reply(env.requests[0]); await until(() => socket.sent.length === 1);
  assert.equal(JSON.stringify(env.api.getStatus()).includes('Investigate the failing'), false);
});

test('unknown existing history fails open without classifying an isolated short reply', async t => {
  const env = environment(t, { metadata: () => [{ plannerConfig: { planModel: MODEL } }] }), socket = new env.Socket(), value = frame();
  value.payload.items = [{ text: 'Fix it.' }]; const original = JSON.stringify(value); socket.send(original);
  await until(() => socket.sent.length === 1);
  assert.deepEqual(socket.sent, [original]); assert.equal(env.requests.length, 0);
});

test('uninstall restores transports, removes overlay and aborts pending work', async t => {
  const env = environment(t), socket = new env.Socket(); socket.send(JSON.stringify(frame()));
  await until(() => env.requests.length === 1); env.api.requestStop(); await until(() => env.api.getStatus().enabled === false);
  assert.equal(env.Socket.prototype.send, env.originalSend); assert.equal(env.sandbox.fetch, env.originalFetch);
  assert.equal(env.elements[0].removed, true); assert.equal(env.statuses.some(x => x.action === 'stop'), true);
  assert.equal(env.sandbox.__clefRouteRespond, undefined); await pause(); assert.equal(socket.sent.length, 0);
});

test('controller validates a bounded native inventory and strips unapproved context', () => {
  const input = { id: randomUUID(), task: 'fixture', mode: 'auto-deep', candidates: [{ model: MODEL, effort: 'low', description: 'native fixture' }],
    context: { images: 0, previous_task: 'prior fixture', nativeHeaders: 'must not pass', previous_route: { model: MODEL, effort: 'low', credentials: 'must not pass' } } };
  assert.equal(validateDecisionRequest(input).candidates[0].effort, 'low');
  assert.equal(JSON.stringify(validateDecisionRequest(input)).includes('must not pass'), false);
  assert.throws(() => validateDecisionRequest({ ...input, candidates: [{ model: 'invented', effort: 'low', description: '' }] }));
  assert.throws(() => validateDecisionRequest({ ...input, task: 'x'.repeat(6000) }));
  assert.deepEqual(validateDecisionRequest({ id: input.id, cancel: true }), { id: input.id, cancel: true });
});

test('decider private stdin invocation accepts only eligible completed routes', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'clef-native-fixture-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const fixture = join(directory, 'decider.mjs');
  await writeFile(fixture, "let s='';for await(const chunk of process.stdin)s+=chunk;const r=JSON.parse(s);console.log(JSON.stringify({...r.candidates[0],source:'clef',usage:{input_tokens:123}}));", { mode: 0o600 });
  const request = validateDecisionRequest({ id: randomUUID(), task: 'private fixture', mode: 'auto', candidates: [{ model: MODEL, effort: 'low', description: 'native fixture' }] });
  const result = await invokeDecider(request, { python: process.execPath, deciderPath: fixture, timeoutMs: 2000 });
  assert.equal(result.model, MODEL); assert.equal(result.usage, undefined);
  await writeFile(fixture, "console.log(JSON.stringify({model:'MODEL_UNKNOWN',effort:'low',source:'clef'}));");
  await assert.rejects(invokeDecider(request, { python: process.execPath, deciderPath: fixture, timeoutMs: 2000 }), /Local routing unavailable/);
});

test('CDP response correlation and disconnection errors remain bounded', async () => {
  class FakeSocket extends EventTarget { sent = []; send(data) { this.sent.push(JSON.parse(data)); } close() { this.dispatchEvent(new Event('close')); } }
  const socket = new FakeSocket(), client = new CDPClient(socket);
  const first = client.call('Runtime.evaluate'), second = client.call('Page.enable');
  socket.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ id: 2, result: { fixture: 2 } }) }));
  socket.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ id: 1, result: { fixture: 1 } }) }));
  assert.deepEqual(await first, { fixture: 1 }); assert.deepEqual(await second, { fixture: 2 });
  const pending = client.call('Page.enable'); const failure = assert.rejects(pending, /Native control disconnected/); socket.close(); await failure;
});
