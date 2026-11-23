const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const app = fs.readFileSync(path.join(__dirname, '../src/polybot2other/static/app.js'), 'utf8');
// Run the dashboard's functions and state without attaching unrelated DOM controls.
const declarations = app.slice(0, app.indexOf('\nfor (const navItem of ids.navItems || [])'));
function harness() {
  const streams = [], requests = [], rendered = [], timers = new Map();
  const context = vm.createContext({
    AbortController, structuredClone, rendered,
    document: { querySelectorAll: () => [], getElementById: () => null, visibilityState: 'visible' },
    EventSource: class {
      constructor(url) { assert.equal(url, '/api/status-stream?format=delta'); streams.push(this); }
      close() { this.closed = true; }
      send(frame) { this.onmessage({ data: JSON.stringify(frame) }); }
    },
    fetch: (url, { signal }) => new Promise((resolve, reject) => {
      assert.equal(url, '/api/status');
      requests.push({ resolve, reject, signal });
      signal.addEventListener('abort', () => reject(new Error('aborted')));
    }),
    setTimeout: (callback, delay) => { timers.set(callback, delay); return callback; },
    clearTimeout: callback => timers.delete(callback),
  });
  context.window = context;
  const run = code => vm.runInContext(code, context);
  run(declarations);
  run(`
    btcRuntimePaused = true;
    samplePageActive = marketPageActive = () => false;
    applyStatusPayload = (data, options) => rendered.push({ data, options });
    loadPolymarketStatus = async () => {};
    renderAll = () => {};
    showError = () => {};
    handleStatusConnectionError = () => {};
  `);
  return { context, run, streams, requests, rendered, timers };
}

const full = (seq, runtime) => ({ type: 'full', seq, data: { runtime }, sent_at_ms: Date.now() });
const response = (runtime, serverAt) => ({
  ok: true, headers: new Headers(serverAt ? { Date: new Date(serverAt).toUTCString() } : {}),
  json: async () => ({ runtime }),
});

test('first frame, nested changes, isolated display state, heartbeat and resync', () => {
  const { run, streams, requests, rendered } = harness();
  assert.equal(run('statusDeliveryAgeMs()'), Infinity);
  run('maintainStatusConnection()');
  assert.equal(requests.length, 0);
  streams[0].send({ ...full(1, { rows: [{ a: 7 }], old: true }), sent_at_ms: Date.now() - 10_000 });
  assert.equal(rendered[0].options.refreshAuxiliary, true);
  rendered[0].data.runtime.rows[0].a = 'filtered UI value';
  streams[0].send({ type: 'delta', seq: 2, base: 1,
    set: [[['runtime', 'rows', 0, 'b'], 2]], remove: [['runtime', 'old']], sent_at_ms: Date.now() - 9000 });
  assert.deepEqual(rendered.at(-1).data.runtime, { rows: [{ a: 7, b: 2 }] });
  assert.equal(rendered.at(-1).options.refreshAuxiliary, false);
  streams[0].send({ type: 'ping', seq: 2, sent_at_ms: Date.now() - 8000 });
  assert.equal(rendered.length, 2);
  assert.ok(run('statusDeliveryAgeMs()') >= 8000);
  streams[0].send(full(3, { reset: true }));
  streams[0].send({ type: 'delta', seq: 5, base: 4, set: [], remove: [] });
  assert.equal(streams[0].closed, true);
  assert.equal(requests.length, 1);
  run('maintainStatusConnection()');
  streams[1].send(full(1, { afterReconnect: true }));
  streams[0].send(full(4, { oldConnection: true }));
  assert.deepEqual(rendered.at(-1).data.runtime, { afterReconnect: true });
});

test('invalid path forces a fresh full frame and partial edits never render', () => {
  const { run, streams, rendered } = harness();
  run('maintainStatusConnection()');
  streams[0].send(full(1, { rows: [{ a: 7 }] }));
  streams[0].send({ type: 'delta', seq: 2, base: 1,
    set: [[['runtime', 'rows', 0, 'a'], 8], [['runtime', 'rows', 2, 'a'], 9]], remove: [] });
  assert.equal(streams[0].closed, true);
  assert.equal(rendered.length, 1);
  assert.equal(rendered[0].data.runtime.rows[0].a, 7);
});

test('slow fallback is single flight and cannot overwrite a newer stream update', async () => {
  const { run, streams, requests, rendered } = harness();
  run('maintainStatusConnection(); lastStatusStreamAt -= STATUS_STREAM_STALE_MS; maintainStatusConnection()');
  const pending = run('loadStatus()');
  assert.equal(run('loadStatus()'), pending);
  assert.equal(requests.length, 1);
  streams[0].send(full(1, { fresh: true }));
  requests[0].resolve(response({ stale: true }));
  await pending;
  assert.equal(rendered.length, 1);
  assert.deepEqual(rendered[0].data.runtime, { fresh: true });
});

test('HTTP timeout releases request and first failure can recover', async () => {
  const { context, run, requests, rendered, timers } = harness();
  context.EventSource = undefined;
  run('maintainStatusConnection()');
  const failed = run('loadStatus()');
  for (const [timeout, delay] of timers) if (delay === 15000) timeout();
  await assert.rejects(failed, /aborted/);
  run('maintainStatusConnection()');
  const next = run('loadStatus()');
  assert.equal(requests.length, 2);
  requests[1].resolve(response({ recovered: true }));
  await next;
  assert.equal(rendered.at(-1).data.runtime.recovered, true);
});

test('hidden page closes transport, ignores late responses and resumes with full frame', async () => {
  const { context, run, streams, requests, rendered } = harness();
  run('maintainStatusConnection()');
  const pending = run('loadStatus()');
  context.document.visibilityState = 'hidden';
  run('handleVisibilityChange(); maintainStatusConnection()');
  assert.equal(streams[0].closed, true);
  assert.equal(requests[0].signal.aborted, true);
  await pending;
  streams[0].send(full(1, { old: true }));
  assert.equal(rendered.length, 0);
  assert.equal(streams.length, 1);
  context.document.visibilityState = 'visible';
  run('handleVisibilityChange()');
  streams[1].send(full(1, { new: true }));
  assert.equal(rendered.at(-1).options.refreshAuxiliary, true);
  assert.equal(requests.length, 1);
});

test('continuously arriving old frames trigger resync and cannot replace fresher HTTP data', async () => {
  const { run, streams, requests, rendered } = harness();
  run('maintainStatusConnection()');
  streams[0].send({ ...full(1, { queue: 'old' }), sent_at_ms: Date.now() - 60_000 });
  run('maintainStatusConnection()');
  const pending = run('loadStatus()');
  streams[0].send({ type: 'ping', seq: 1, sent_at_ms: Date.now() - 59_000 });
  requests[0].resolve(response({ queue: 'new' }, Date.now()));
  await pending;
  assert.deepEqual(rendered.at(-1).data.runtime, { queue: 'new' });
  streams[0].send({ ...full(2, { queue: 'still old' }), sent_at_ms: Date.now() - 58_000 });
  assert.deepEqual(rendered.at(-1).data.runtime, { queue: 'new' });
  assert.ok(run('statusDeliveryAgeMs()') < 2000);
  run('maintainStatusConnection()');
  assert.equal(streams[0].closed, true);
  streams[1].send(full(1, { queue: 'current' }));
  assert.deepEqual(rendered.at(-1).data.runtime, { queue: 'current' });
});

test('same-origin server clock prevents reconnect loops when browser clock is ahead', async () => {
  const { run, streams, requests } = harness();
  run('maintainStatusConnection()');
  const serverAt = Date.now() - 300_000;
  streams[0].send({ ...full(1, { fresh: true }), sent_at_ms: serverAt });
  run('maintainStatusConnection()');
  const pending = run('loadStatus()');
  requests[0].resolve(response({ fresh: true }, serverAt));
  await pending;
  streams[0].send({ type: 'ping', seq: 1, sent_at_ms: serverAt + 1 });
  run('maintainStatusConnection()');
  assert.equal(streams.length, 1);
  assert.equal(requests.length, 1);
  assert.ok(Math.abs(run('statusServerNowMs()') - serverAt) < 1500);
  assert.ok(run('statusDeliveryAgeMs()') < 1500);
});
