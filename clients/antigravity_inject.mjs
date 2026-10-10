/** Installed in the native renderer; native authentication never leaves it. */
export function installClefRouter(config = {}) {
  const root = globalThis;
  const key = '__clefAntigravityAuto';
  if (root.top && root.top !== root) return { installed: false, reason: 'subframe' };
  if (root[key]?.enabled) return { installed: true, existing: true };
  const modes = { 'auto-cheap': 'Auto Cheap', auto: 'Auto Standard', 'auto-deep': 'Auto Deep' };
  const binding = config.bindingName || '__clefRouteRequest';
  const statusBinding = config.statusBindingName || '__clefRouteStatus';
  const responseName = config.responseName || '__clefRouteRespond';
  const timeoutMs = Math.max(10, Math.min(25000, Number(config.timeoutMs) || 20000));
  const service = '/exa.language_server_pb.LanguageServerService/';
  const procedure = service + 'SendUserCascadeMessage';
  const originalSend = root.WebSocket.prototype.send;
  const originalFetch = root.fetch;
  const jobs = new Map(), socketJobs = new WeakMap(), contexts = new Map();
  const state = { enabled: true, mode: modes[config.mode] ? config.mode : 'auto', records: [], last: null };
  let catalogue = [], lastDispatch = null, sequence = 0;
  let overlay = null, button = null, label = null;
  const trim = (s, n) => typeof s !== 'string' ? '' : s.length <= n ? s : s.slice(0, n / 2) + '\n[…]\n' + s.slice(-n / 2);
  const effortOf = text => text.match(/\((low|medium|high|xhigh|max)\)\s*$/i)?.[1].toLowerCase() ?? null;
  function view(text) {
    if (button) button.textContent = modes[state.mode];
    if (label) label.textContent = text;
  }
  function record(job, outcome, candidate, reason) {
    const item = { id: job.id, at: Date.now(), mode: job.mode, outcome,
      source: outcome === 'dispatched' ? 'clef' : 'native', ms: Date.now() - job.started };
    if (candidate) Object.assign(item, { model: candidate.model, effort: candidate.effort, label: candidate.label });
    if (reason) item.reason = reason;
    state.last = item; state.records.push(item);
    if (state.records.length > 128) state.records.shift();
    try { root[statusBinding]?.(JSON.stringify(item)); } catch { /* Metadata reporting cannot block a turn. */ }
    if (outcome === 'dispatched') view(candidate.label);
    else if (outcome === 'fallback') view('Native selection · routing unavailable');
    else if (outcome === 'cancelled') view('Cancelled');
    else if (outcome === 'skipped') view('Custom model · native selection');
  }
  function aborted(job) {
    if (!state.enabled || job.controller.signal.aborted) throw new Error(job.reason || 'cancelled');
  }
  function raceAbort(promise, job) {
    return new Promise((resolve, reject) => {
      const signal = job.controller.signal;
      const cancel = () => reject(new Error(job.reason || 'cancelled'));
      if (signal.aborted) { cancel(); return; }
      signal.addEventListener('abort', cancel, { once: true });
      Promise.resolve(promise).then(resolve, reject).finally(() => signal.removeEventListener('abort', cancel));
    });
  }
  function cancel(job, reason = 'cancelled') {
    if (job.controller.signal.aborted) return;
    job.reason = reason; job.controller.abort();
    if (job.requested) {
      try { root[binding]?.(JSON.stringify({ id: job.id, cancel: true })); } catch { /* Already disconnected. */ }
    }
  }
  function createJob(payload) {
    const job = { id: (root.crypto?.randomUUID?.() || `clef-${Date.now()}-${++sequence}`),
      mode: state.mode, started: Date.now(), controller: new AbortController(), payload };
    job.timer = setTimeout(() => cancel(job, 'timeout'), timeoutMs);
    jobs.set(job.id, job); view('Choosing model…');
    return job;
  }
  function finish(job) {
    clearTimeout(job.timer); jobs.delete(job.id); job.resolve = null;
  }
  function nativeHeaders() {
    const token = root.__APP_CONFIG__?.csrfToken;
    if (typeof token !== 'string' || !token) throw new Error('catalogue-unavailable');
    return { 'Content-Type': 'application/json', 'Connect-Protocol-Version': '1', 'x-codeium-csrf-token': token };
  }
  async function nativeRead(method, body, signal) {
    const response = await originalFetch.call(root, root.location.origin + service + method,
      { method: 'POST', headers: nativeHeaders(), body: JSON.stringify(body), signal });
    if (!response.ok) throw new Error('native-read-unavailable');
    // Native history stays in this renderer. Cap transport bytes before parsing,
    // even when an offset-only RPC unexpectedly returns a large tail.
    if (!response.body?.getReader) return response.json();
    const reader = response.body.getReader(), chunks = []; let bytes = 0;
    try {
      for (;;) {
        const chunk = await reader.read(); if (chunk.done) break;
        bytes += chunk.value.byteLength;
        if (bytes > 1024 * 1024) { await reader.cancel(); throw new Error('native-read-too-large'); }
        chunks.push(chunk.value);
      }
    } finally { reader.releaseLock(); }
    const data = new Uint8Array(bytes); let offset = 0;
    for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.byteLength; }
    return JSON.parse(new TextDecoder().decode(data));
  }
  async function getCatalogue(job) {
    const result = await raceAbort(nativeRead('GetCascadeModelConfigData', {}, job.controller.signal), job);
    if (!Array.isArray(result?.clientModelConfigs) || result.clientModelConfigs.length > 160) throw new Error('catalogue-unavailable');
    const seen = new Set();
    catalogue = result.clientModelConfigs.filter(x => x && x.disabled !== true && typeof x.label === 'string')
      .map(x => ({ model: x.modelOrAlias?.model, effort: effortOf(x.label), label: trim(x.label, 300),
        modelId: typeof x.modelId === 'string' && /^[a-zA-Z0-9_.:/-]{1,160}$/.test(x.modelId) ? x.modelId : null,
        supportsImages: x.supportsImages === true,
        description: trim(x.description || '', 300) + (x.supportsImages === true ? ' Supports images.' : ' Image support not advertised.') }))
      .filter(x => typeof x.model === 'string' && /^MODEL_[A-Z0-9_]{1,150}$/.test(x.model)
        && !seen.has(x.model) && (seen.add(x.model), true));
    if (!catalogue.length || catalogue.length > 80) throw new Error('catalogue-unavailable');
    return catalogue;
  }
  function taskOf(payload) {
    if (!Array.isArray(payload.items)) return '';
    return trim(payload.items.slice(-80).map(x => typeof x?.text === 'string' ? x.text : '').filter(Boolean).join('\n'), 5000).trim();
  }
  function metadataList(data) {
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('metadata-unavailable');
    // Protobuf JSON omits an empty repeated field. An otherwise empty object is
    // a valid fresh conversation, whereas malformed/error objects remain errors.
    if (Object.keys(data).length === 0) return [];
    if ('error' in data || !Array.isArray(data.generatorMetadata) || data.generatorMetadata.length > 10000) throw new Error('metadata-unavailable');
    return data.generatorMetadata;
  }
  async function metadataOf(job) {
    const data = await raceAbort(nativeRead('GetCascadeTrajectoryGeneratorMetadata',
      { cascadeId: job.payload.cascadeId, includeMessages: false }, job.controller.signal), job);
    return metadataList(data);
  }
  async function recentContext(job, metadata) {
    if (!metadata.length) return [];
    const indices = metadata.slice(-8).flatMap(x => Array.isArray(x?.stepIndices) ? x.stepIndices : [])
      .filter(x => Number.isInteger(x) && x >= 0 && x <= 10000000);
    if (!indices.length) throw new Error('context-unavailable');
    const data = await raceAbort(nativeRead('GetCascadeTrajectorySteps', { cascadeId: job.payload.cascadeId,
      stepOffset: Math.max(0, Math.max(...indices) - 12), trajectoryVerbosity: 'CLIENT_TRAJECTORY_VERBOSITY_PROD_UI',
      disableRehydration: true }, job.controller.signal), job);
    if (!Array.isArray(data?.steps) || data.steps.length > 64) throw new Error('context-unavailable');
    const recent = [], roles = { user: 0, assistant: 0 };
    for (const step of data.steps.slice(-16).reverse()) {
      let role, text;
      if (step?.userInput) {
        role = 'user'; text = step.userInput.query || (step.userInput.items || []).slice(-20).map(x => x?.text || '').join('\n');
      } else if (step?.plannerResponse) { role = 'assistant'; text = step.plannerResponse.modifiedResponse || step.plannerResponse.response; }
      if (role && typeof text === 'string' && text.trim() && roles[role] < 2) { roles[role]++; recent.push({ role, text: trim(text, 650) }); }
    }
    if (!recent.length) throw new Error('context-unavailable');
    return recent.reverse();
  }
  function decision(job, task, candidates, context) {
    if (typeof root[binding] !== 'function') throw new Error('controller-unavailable');
    const answer = new Promise(resolve => { job.resolve = resolve; });
    job.requested = true;
    root[binding](JSON.stringify({ id: job.id, task, candidates: candidates.map(({ model, effort, label: friendly, description }) =>
      ({ model, effort, description: trim(friendly + '. ' + description, 400) })), mode: job.mode, context }));
    return raceAbort(answer, job);
  }
  async function route(job) {
    const payload = job.payload, task = taskOf(payload);
    if (!task) throw new Error('no-task');
    const inventory = await getCatalogue(job);
    aborted(job);
    const imageCount = Array.isArray(payload.images) ? payload.images.length : 0;
    const candidates = imageCount ? inventory.filter(x => x.supportsImages) : inventory;
    if (!candidates.length) throw new Error('catalogue-unavailable');
    const prior = contexts.get(payload.cascadeId);
    let recent = [], baselineMetadata;
    try { baselineMetadata = await metadataOf(job); recent = await recentContext(job, baselineMetadata); }
    catch { if (!prior) throw new Error('context-unavailable'); }
    const context = { images: imageCount, media: Array.isArray(payload.media) ? payload.media.length : 0,
      ...(recent.length ? { recent_turns: recent } : {}),
      ...(prior ? { previous_task: prior.task, previous_route: prior.route } : {}) };
    const result = await decision(job, task, candidates, context);
    aborted(job);
    if (state.mode !== job.mode) throw new Error('mode-changed');
    if (result?.source !== 'clef') throw new Error('invalid-route');
    const candidate = candidates.find(x => x.model === result.model && x.effort === (result.effort ?? null));
    if (!candidate) throw new Error('invalid-route');
    const current = await getCatalogue(job);
    aborted(job);
    if (!current.some(x => x.model === candidate.model && x.effort === candidate.effort
      && (!imageCount || x.supportsImages))) throw new Error('stale-route');
    const plannerConfig = { ...payload.cascadeConfig?.plannerConfig,
      planModel: candidate.model, requestedModel: { model: candidate.model } };
    delete plannerConfig.modelName;
    delete plannerConfig.customModelInfoOverride;
    const changed = { ...payload, cascadeConfig: { ...payload.cascadeConfig, plannerConfig } };
    let baselineCount = null;
    try {
      const metadata = await metadataOf(job); baselineCount = metadata.length;
    } catch { /* Dispatch remains available when native proof metadata is unavailable. */ }
    aborted(job);
    return { payload: changed, candidate, task, baselineCount };
  }
  function remember(job, routed) {
    const conversation = job.payload.cascadeId;
    lastDispatch = { conversation, model: routed.candidate.model, effort: routed.candidate.effort,
      modelId: routed.candidate.modelId, baselineCount: routed.baselineCount };
    if (typeof conversation === 'string') {
      contexts.delete(conversation);
      contexts.set(conversation, { task: trim(routed.task, 1800), route: { model: routed.candidate.model, effort: routed.candidate.effort } });
      if (contexts.size > 128) contexts.delete(contexts.keys().next().value);
    }
    record(job, 'dispatched', routed.candidate);
  }
  function parseFrame(data) {
    if (typeof data !== 'string' || data.length > 16 * 1024 * 1024) return null;
    try { return JSON.parse(data); } catch { return null; }
  }
  function grpcJsonBody(body) {
    // Native unary grpc-web+json uses one uncompressed message: flag byte,
    // big-endian byte length, then UTF-8 JSON. Never interpret proto, trailers,
    // compressed messages or multiple frames as a routing request.
    const limit = 16 * 1024 * 1024;
    if (!ArrayBuffer.isView(body) || Object.prototype.toString.call(body) !== '[object Uint8Array]'
      || body.byteLength < 5 || body.byteLength > limit + 5 || body[0] !== 0) return null;
    const view = new DataView(body.buffer, body.byteOffset, body.byteLength);
    if (view.getUint32(1) !== body.byteLength - 5) return null;
    const payload = parseFrame(new TextDecoder('utf-8', { fatal: true }).decode(body.subarray(5)));
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
    return { payload, encode(value) {
      const json = new TextEncoder().encode(JSON.stringify(value));
      if (json.byteLength > limit) throw new Error('request-too-large');
      const changed = new Uint8Array(json.byteLength + 5);
      changed.set(json, 5); new DataView(changed.buffer).setUint32(1, json.byteLength);
      return changed;
    } };
  }
  async function fetchBody(input, init) {
    const contentType = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined))
      .get('content-type')?.split(';', 1)[0].trim().toLowerCase();
    if (contentType === 'application/grpc-web+json') {
      const body = init?.body === undefined && input instanceof Request
        ? new Uint8Array(await input.clone().arrayBuffer()) : init?.body;
      return grpcJsonBody(body);
    }
    const text = typeof init?.body === 'string' ? init.body
      : init?.body === undefined && input instanceof Request ? await input.clone().text() : null;
    const payload = parseFrame(text);
    return payload && typeof payload === 'object' && !Array.isArray(payload)
      ? { payload, encode: JSON.stringify } : null;
  }
  function customModel(payload) {
    const planner = payload?.cascadeConfig?.plannerConfig;
    return !!planner?.customModelInfoOverride || /BYOM/.test(planner?.planModel || planner?.requestedModel?.model || '');
  }
  function skipped(payload) {
    const job = { id: root.crypto?.randomUUID?.() || `clef-${Date.now()}-${++sequence}`, mode: state.mode, started: Date.now() };
    record(job, 'skipped', null, 'custom-model');
  }
  function patchedSend(data) {
    const frame = parseFrame(data);
    const perSocket = socketJobs.get(this);
    if (frame?.type === 'cancel' && perSocket?.has(frame.streamId)) cancel(perSocket.get(frame.streamId));
    if (!state.enabled || frame?.type !== 'start' || frame.stream !== false || frame.procedure !== procedure
      || !frame.payload || typeof frame.payload !== 'object' || jobs.size >= 8) return originalSend.call(this, data);
    if (customModel(frame.payload)) { skipped(frame.payload); return originalSend.call(this, data); }
    const socket = this, job = createJob(frame.payload);
    let queued = perSocket;
    if (!queued) {
      queued = new Map(); socketJobs.set(socket, queued);
      socket.addEventListener('close', () => { for (const pending of queued.values()) cancel(pending); }, { once: true });
    }
    if (queued.has(frame.streamId)) cancel(queued.get(frame.streamId));
    queued.set(frame.streamId, job);
    (async () => {
      try {
        const routed = await route(job);
        aborted(job);
        originalSend.call(socket, JSON.stringify({ ...frame, payload: routed.payload }));
        remember(job, routed);
      } catch {
        if (job.reason === 'cancelled' || !state.enabled || socket.readyState !== root.WebSocket.OPEN) record(job, 'cancelled');
        else { originalSend.call(socket, data); record(job, 'fallback', null, job.reason || 'routing-unavailable'); }
      } finally { if (queued.get(frame.streamId) === job) queued.delete(frame.streamId); finish(job); }
    })().catch(() => { /* Native socket errors must not create an unhandled rejection. */ });
  }
  async function patchedFetch(input, init) {
    let url;
    try { url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url, root.location.href); } catch { return originalFetch.call(root, input, init); }
    const method = (init?.method || input?.method || 'GET').toUpperCase();
    if (!state.enabled || url.origin !== root.location.origin || url.pathname !== procedure || method !== 'POST' || jobs.size >= 8)
      return originalFetch.call(root, input, init);
    let body;
    try { body = await fetchBody(input, init); } catch { body = null; }
    if (!body) return originalFetch.call(root, input, init);
    const payload = body.payload;
    if (customModel(payload)) { skipped(payload); return originalFetch.call(root, input, init); }
    const job = createJob(payload), signal = init?.signal || input?.signal;
    const onAbort = () => cancel(job);
    signal?.addEventListener('abort', onAbort, { once: true });
    if (signal?.aborted) cancel(job);
    try {
      const routed = await route(job);
      aborted(job);
      const changedInit = { ...init, body: body.encode(routed.payload) };
      remember(job, routed);
      // Returning this promise directly prevents a native transport rejection from
      // entering the classifier fallback and resubmitting the user turn.
      return originalFetch.call(root, input instanceof Request ? new Request(input, changedInit) : input,
        input instanceof Request ? undefined : changedInit);
    } catch (error) {
      if (job.reason === 'cancelled' || !state.enabled) { record(job, 'cancelled'); throw new DOMException('Aborted', 'AbortError'); }
      record(job, 'fallback', null, job.reason || 'routing-unavailable');
      return originalFetch.call(root, input, init);
    } finally { signal?.removeEventListener('abort', onAbort); finish(job); }
  }
  function respond(id, result) {
    const job = jobs.get(id);
    if (!job?.resolve || job.controller.signal.aborted) return false;
    const resolve = job.resolve; job.resolve = null; resolve(result); return true;
  }
  const previousResponse = root[responseName];
  root[responseName] = respond;
  root.WebSocket.prototype.send = patchedSend;
  root.fetch = patchedFetch;
  function mountOverlay() {
    if (!state.enabled || overlay) return;
    if (!root.document?.documentElement) {
      root.document?.addEventListener?.('DOMContentLoaded', mountOverlay, { once: true }); return;
    }
    try {
      overlay = root.document.createElement('div'); overlay.id = 'clef-antigravity-auto';
      Object.assign(overlay.style, { position: 'fixed', bottom: '12px', left: '12px', zIndex: '2147483646',
        padding: '8px 12px', borderRadius: '10px', background: '#202124', color: '#f1f3f4', font: '12px system-ui', display: 'flex', gap: '10px' });
      button = root.document.createElement('button'); button.type = 'button'; button.title = 'Change Auto preference';
      button.addEventListener('click', () => api.setMode(Object.keys(modes)[(Object.keys(modes).indexOf(state.mode) + 1) % 3]));
      label = root.document.createElement('span'); overlay.append(button, label); root.document.documentElement.append(overlay);
      view(state.last?.label || 'Ready');
    } catch { /* Routing is still functional without the optional overlay. */ }
  }
  const api = {
    enabled: true,
    setMode(mode) { if (!modes[mode]) return false; state.mode = mode; view(state.last?.label || 'Ready'); return true; },
    getStatus() { return JSON.parse(JSON.stringify({ enabled: state.enabled, mode: state.mode, last: state.last, records: state.records })); },
    async verifyLastRoute() {
      if (!lastDispatch || lastDispatch.baselineCount === null) return { verified: false, reason: 'no-dispatched-route' };
      const dispatch = { ...lastDispatch }, selected = dispatch.model, abort = new AbortController();
      const timer = setTimeout(() => abort.abort(), 5000);
      try {
        const data = await nativeRead('GetCascadeTrajectoryGeneratorMetadata', { cascadeId: dispatch.conversation, includeMessages: false }, abort.signal);
        const all = metadataList(data);
        const latest = all.at(-1), planner = latest?.plannerConfig, actual = latest?.chatModel;
        const actualEnum = typeof actual?.model === 'string' ? actual.model : null;
        const nativeModel = actualEnum || planner?.planModel || planner?.requestedModel?.model;
        const names = [actual?.responseModelFull, actual?.responseModel, planner?.modelName].filter(x => typeof x === 'string' && x);
        const expected = dispatch.modelId ? [dispatch.modelId] : [];
        // A runtime may omit its effort suffix; the observed execution enum must
        // independently confirm effort before accepting that base identifier.
        if (dispatch.modelId && dispatch.effort && actualEnum === selected && dispatch.modelId.endsWith('-' + dispatch.effort))
          expected.push(dispatch.modelId.slice(0, -dispatch.effort.length - 1));
        const namesAgree = names.length > 0 && expected.length > 0 && names.every(x => expected.includes(x));
        const actualEvidence = actualEnum === selected || namesAgree;
        const contrary = (actualEnum !== null && actualEnum !== selected) || (names.length > 0 && !namesAgree);
        const verified = all.length > dispatch.baselineCount && actualEvidence && !contrary;
        return { verified, ...(verified ? {} : { reason: contrary ? 'model-identifier-unverified' : 'fresh-model-evidence-unavailable' }),
          selectedModel: selected, nativeModel: trim(nativeModel, 160), expectedModelId: dispatch.modelId,
          modelName: trim(planner?.modelName, 160), responseModel: trim(actual?.responseModel, 160), responseModelFull: trim(actual?.responseModelFull, 160),
          generatorMetadataCount: all.length, baselineGeneratorMetadataCount: dispatch.baselineCount };
      } catch { return { verified: false, reason: 'metadata-unavailable' }; }
      finally { clearTimeout(timer); }
    },
    requestStop() { api.uninstall(); try { root[statusBinding]?.(JSON.stringify({ action: 'stop' })); } catch { /* Controller disconnected. */ } return true; },
    uninstall() {
      if (!state.enabled) return;
      state.enabled = false; api.enabled = false;
      for (const job of jobs.values()) cancel(job);
      if (root.WebSocket.prototype.send === patchedSend) root.WebSocket.prototype.send = originalSend;
      if (root.fetch === patchedFetch) root.fetch = originalFetch;
      if (root[responseName] === respond) { if (previousResponse === undefined) delete root[responseName]; else root[responseName] = previousResponse; }
      root.document?.removeEventListener?.('DOMContentLoaded', mountOverlay);
      overlay?.remove(); contexts.clear(); lastDispatch = null;
    },
  };
  root[key] = api;
  mountOverlay();
  return { installed: true, mode: state.mode };
}
