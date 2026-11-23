const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { test } = require("node:test");

const staticDir = path.resolve(__dirname, "../src/polybot2other/static");

function liveStatus(deliveryAge, runtimeEnabled, executionState) {
  const env = {
    document: { addEventListener() {} }, window: {}, setInterval() {},
    statusDeliveryAgeMs: () => deliveryAge, statusServerNowMs: Date.now, accountScope: "main"
  };
  vm.runInNewContext(readFileSync(path.join(staticDir, "managed-live.js"), "utf8"), env);
  return env.window.ManagedLiveUI.execution({runtime: { managed_live: {
    runtime_enabled: runtimeEnabled,
    market_data: { observed_at: Date.now() / 1000 - 90 },
    variants: [{ variant_id: "MAIN", execution: {state: executionState, label: "等待入场条件"} }]
  } }});
}

test("a stale market timestamp does not declare a connected live service offline", () => {
  assert.equal(liveStatus(800, true, "waiting").state, "waiting");
  assert.equal(liveStatus(800, false, "off").label, "实盘已关闭");
});

test("a lost status feed preserves the last known hard-off setting and flags an unverified live account", () => {
  const off = liveStatus(16_000, false, "off");
  assert.equal(off.state, "off");
  assert.match(off.label, /上次确认/);
  for (const flag of [true, undefined]) {
    const live = liveStatus(16_000, flag, "waiting");
    assert.equal(live.state, "blocked");
    assert.equal(live.disconnected, true);
  }
});

function paperStatus(priceAge, deliveryAge) {
  const src = readFileSync(path.join(staticDir, "app.js"), "utf8");
  const code = src.slice(src.indexOf("function renderRuntime("), src.indexOf("function renderLivePanel("));
  const runtime = { dataset: {}, classList: { toggle() {} } };
  const env = {
    window: {}, ids: { runtime, lastTick: {} }, btcRuntimePaused: false,
    statusDeliveryAgeMs: () => deliveryAge, statusServerNowMs: Date.now,
    data: { market_input_alignment: { enabled: true, ready: true }, paper_price: priceAge == null
      ? {} : { chainlink: 60_000, chainlink_updated_ms: Date.now() - priceAge } }
  };
  vm.runInNewContext(code + "\nrenderRuntime(data);", env);
  return runtime;
}

test("Paper separately identifies a fresh price, a late price, no price, and a lost connection", () => {
  assert.equal(paperStatus(200, 200).dataset.state, "running");
  assert.equal(paperStatus(7_000, 200).textContent, "BTC 报价延迟");
  assert.match(paperStatus(null, 200).textContent, /报价/);
  assert.equal(paperStatus(7_000, 18_000).textContent, "页面正在重连");
});
