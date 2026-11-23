const {test} = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const source = readFileSync(path.join(__dirname, "../src/polybot2other/static/first-strong-account.js"), "utf8");
const app = readFileSync(path.join(__dirname, "../src/polybot2other/static/app.js"), "utf8");
const id = "MAIN_FIRST_STRONG";
const scope = "experiment:" + id;
function functionsFromApp(env, names) {
  for (const name of names) {
    const match = app.match(new RegExp("^(?:async )?function " + name + "\\([\\s\\S]*?^}", "m"));
    assert.ok(match, name);
    vm.runInContext(match[0], env);
  }
}

test("the independent Paper selector, curve, fills and trades use only the dedicated snapshot", async () => {
  let time = 200_000, reply, updates = 0;
  const calls = [];
  const data = {runtime:{strategy_experiments:{variants:[{variant_id:"OLD",combo:"原有账户"}]}}};
  const env = vm.createContext({window:{dispatchEvent(){updates++;}}, document:{hidden:false}, Date:{now:()=>time},
    fetch:async(path,options)=>{calls.push([path,options.method || "GET"]);return {ok:true,json:async()=>reply};},
    AbortController, Event, setTimeout, clearTimeout, EXIT_SETTLE_ID:"FORECAST_EXIT_SETTLE", accountScope:scope,
    latestStatus:data, exitSettlePaper:null, exitSettlePaperError:"", isDeprecatedStrategyVariant:()=>false,
    chartInitialBalance:()=>100, normalizeEquityPoint:r=>r, downsampleEquityRows:r=>r, EQUITY_CURVE_MAX_POINTS:500,
    orderDataScope:scope, recentDataScope:scope, orderStatusFilter:"filled", recentFilters:{start_at:null,end_at:null},
    renderRecentOrders(){},renderRecentTrades(){},
  });
  vm.runInContext(source, env);
  const paper = env.window.FirstStrongPaper;
  functionsFromApp(env, ["strategyExperimentVariants","selectedAccountVariant","parseAccountScope","selectedAccountMetrics",
    "isFirstStrongScope","hasSeparatePaperLedger","isExitSettleScope","buildChartRows","scopedOpenRows","loadOrders","loadRecentTradesPage","loadEquityCurve","selectedAccountCurrentPoint"]);
  assert.equal(env.selectedAccountVariant(data).variant_id, id);
  assert.equal(env.selectedAccountMetrics(data).total_equity, undefined);
  assert.match(paper.warning, /正在连接/);
  reply = {key:id, mode:"paper", as_of:200, state:"collecting",
    account:{initial:100,equity_at_cost:101,realized:1,cash:96,open_positions:1,open_cost:5,reserved:0,settled:2,wins:1,win_rate:50},
    equity_curve:[{at:100,equity:100},{at:120,equity:104},{at:140,equity:101}],
    recent_trades:[
      {round_id:"open",side:"Up",opened_at:150,stake:5,status:"OPEN"},
      {round_id:"late",side:"Down",opened_at:128,settled_at:140,pnl:-3,stake:3,status:"SETTLED"},
      {round_id:"early",side:"Up",opened_at:105,settled_at:120,pnl:4,stake:4,status:"SETTLED"}],
    recent_orders:[{id:2,status:"PENDING"},{id:1,status:"PARTIAL"}],
    last_event:{message:"等待盘口"}};
  paper.refresh();
  paper.refresh();
  await new Promise(setImmediate);
  assert.equal(updates,1);
  assert.equal(paper.warning,"");
  assert.equal(paper.variant().metrics.max_drawdown,3);
  assert.equal(paper.variant().metrics.unrealized_pnl,null);
  assert.equal(paper.reason,"等待盘口");
  assert.deepEqual(Array.from(env.scopedOpenRows(scope),t=>t.id),["open"]);
  assert.deepEqual(Array.from(env.buildChartRows([{created_at:199,total_equity:888}]),p=>p.total_equity),[100,104,101]);
  assert.equal(env.selectedAccountCurrentPoint(), null);
  await env.loadEquityCurve(true);
  await env.loadOrders(true);
  await env.loadRecentTradesPage(true);
  assert.deepEqual(Array.from(env.orderRows, o=>o.id),[1]);
  assert.deepEqual(Array.from(env.recentRows, t=>t.id),["open","late","early"]);
  assert.equal(env.recentSummary.total_pnl,1);
  assert.equal(paper.tradePage({start_at:140,end_at:140}).summary.total_pnl,-3);
  assert.deepEqual(calls,[["/api/first-strong-paper","GET"]]);
  time += 10_000;
  reply = {mode:"live",key:"MAIN",account:{equity_at_cost:999}};
  paper.refresh();
  await new Promise(setImmediate);
  assert.equal(env.selectedAccountMetrics(data).total_equity,101);
  assert.match(paper.warning,/连接中断/);
});
