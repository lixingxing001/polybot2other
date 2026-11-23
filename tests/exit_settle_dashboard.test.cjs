const {readFileSync} = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const {test} = require("node:test");

const staticDir = path.resolve(__dirname, "../src/polybot2other/static");
const script = name => readFileSync(path.join(staticDir, name), "utf8");
const app = script("app.js");
const id = "FORECAST_EXIT_SETTLE";
const scope = "experiment:" + id;
function loadFunctions(names, bindings) {
  const code = ["isFirstStrongScope", "hasSeparatePaperLedger", ...names].map(name => {
    const match = app.match(new RegExp("^(?:async )?function " + name + "\\([\\s\\S]*?^}", "m"));
    assert.ok(match, name);
    return match[0];
  }).join("\n");
  const env = vm.createContext({window:{}, EXIT_SETTLE_ID:id, scope, ...bindings});
  vm.runInContext(code, env);
  return env;
}

test("the original candidate account is listed once and selected tables follow it", () => {
  const selectors = {accountScopeSelect:{value:scope}, openDataScope:{}, orderDataScope:{}, recentDataScope:{}, chartTooltip:{}};
  const work = [];
  const data = {runtime:{strategy_experiments:{variants:[{variant_id:"OTHER", combo:"另一账户"}]}}};
  const paper = {variant_id:id, combo:"拿到官方结算", metrics:{realized_pnl:15}, open_trades:[{round_id:"old"}]};
  const env = loadFunctions(["strategyExperimentVariants", "accountScopeOptions", "isExitSettleScope",
    "handleAccountScopeChange", "scopedOpenRows"], {
    ids:selectors, accountScope:"main", openDataScope:"main", orderDataScope:"main", recentDataScope:"main",
    isDeprecatedStrategyVariant:()=>false, latestStatus:data, exitSettlePaper:{enabled:true,variant:paper},
    exitSettlePaperError:"", handleDataScopeChange:(kind, selected)=>work.push([kind, selected]),
    renderAll(){}, loadEquityCurve:async()=>{}, showError(){},
  });
  assert.equal(env.accountScopeOptions(data).filter(option=>option.value === scope).length, 1);
  env.handleAccountScopeChange();
  assert.deepEqual(work, [["open",scope], ["order",scope], ["recent",scope]]);
  assert.deepEqual(env.scopedOpenRows(scope), paper.open_trades);
  data.runtime.strategy_experiments.variants.push(paper);
  assert.equal(env.accountScopeOptions(data).filter(option=>option.value === scope).length, 1);
});

test("Paper uses the historical settlement curve without appending a marked-to-market point", async () => {
  let request;
  const ledger = [{created_at:100,total_equity:100,realized_pnl:0},{created_at:200,total_equity:115,realized_pnl:15}];
  const env = loadFunctions(["loadEquityCurve","buildChartRows","selectedAccountCurrentPoint","isExitSettleScope",
    "parseAccountScope","drawSettlementPath"], {
    latestStatus:{metrics:{total_equity:999}}, accountScope:scope, window:{},
    URLSearchParams, EQUITY_CURVE_DAYS:90, EQUITY_CURVE_MAX_POINTS:500, EQUITY_CURVE_REFRESH_MS:30_000,
    equityCurveInFlight:false, equityCurvePendingForce:false, lastEquityCurveFetchMs:0,
    renderAll(){}, chartInitialBalance:()=>100, normalizeEquityPoint:p=>p, downsampleEquityRows:r=>r,
    fetch:async url => {request = new URL(url, "http://127.0.0.1"); return {ok:true,json:async()=>({equity_curve:ledger})};},
  });
  assert.equal(env.selectedAccountCurrentPoint(), null);
  await env.loadEquityCurve(true);
  assert.equal(request.pathname,"/api/equity-curve");
  assert.equal(request.searchParams.get("account_scope"),"strategy_experiment");
  assert.equal(request.searchParams.get("variant_id"),id);
  assert.equal(request.searchParams.get("full_history"),"1");
  assert.deepEqual(Array.from(env.buildChartRows()), ledger);
  const segments=[];
  env.drawSettlementPath({lineTo:(x,y)=>segments.push([x,y])},[{x:1,y:3},{x:5,y:7}]);
  assert.deepEqual(segments,[[5,3],[5,7]]);
});

test("viewing a disabled live strategy displays only its own closed positions and never posts", () => {
  const handlers={};
  const env=vm.createContext({window:{}, document:{addEventListener:(type,fn)=>{handlers[type]=fn;}},
    setInterval(){}, accountScope:scope, EXIT_SETTLE_ID:id, parseAccountScope:()=>({variantId:id}),
    isForecastPaperStrategy:()=>false, renderAll(){}, requestAnimationFrame:fn=>fn(), loadEquityCurve:async()=>{},showError(){},
    fetch:()=>{throw Error("Read-only view attempted a write");}
  });
  vm.runInContext(script("managed-live.js"), env);
  const now=Date.now()/1000, start=now-12*86400;
  const ui=env.window.ManagedLiveUI, data={runtime:{managed_live:{enabled:true,settings:{strategies:{
    MAIN:{enabled:true},[id]:{enabled:false}}},wallet_curve:[{created_at:start,total_equity:999},{created_at:now-1,total_equity:1234}],
    variants:[{variant_id:"MAIN",settled_pnl_curve:[{created_at:100,realized_pnl:99}]},
              {variant_id:id,settled_pnl_curve:[]}]}}};
  env.latestStatus=data;
  assert.equal(ui.active(data),false);
  handlers.change({target:{id:"managed-data-view",value:"live"}});
  assert.equal(ui.active(data),true);
  assert.equal(ui.curve(data).length,0);
  data.runtime.managed_live.variants[1].settled_pnl_curve.push({created_at:start,realized_pnl:0},{created_at:now-1,realized_pnl:-2});
  assert.deepEqual(Array.from(ui.curve(data),p=>p.total_equity),[0,-2]);
  assert.deepEqual(Array.from(ui.curve(data),p=>p.created_at),[start,now-1]);
  assert.equal(ui.currentPoint(data),null);
  env.accountScope="main";
  assert.deepEqual(Array.from(ui.curve(data),p=>p.total_equity),[1234]);
  env.accountScope=scope;
  handlers.change({target:{id:"managed-data-view",value:"paper"}});
  assert.equal(ui.active(data),false);
  assert.equal(data.runtime.managed_live.settings.strategies[id].enabled,false);
});

test("identical Paper order numbers across wallets keep distinct fills and use their scoped API", async () => {
  const calls=[];
  const env=loadFunctions(["toggleOrderFills","appendScopeParams","parseAccountScope"],{
    URLSearchParams,orderDataScope:"main",expandedOrderId:null,loadingOrderId:null,
    orderFillCache:new Map(),orderRows:[],renderRecentOrders(){},
    fetch:async path=>{const url=new URL(path,"http://127.0.0.1");calls.push(url);
      return {ok:true,json:async()=>({fills:[{shares:calls.length}]})};}
  });
  await env.toggleOrderFills(1);
  env.expandedOrderId=null;
  env.orderDataScope=scope;
  await env.toggleOrderFills(1);
  assert.deepEqual(calls.map(url=>url.pathname),["/api/order-fills","/api/order-fills"]);
  assert.equal(calls[1].searchParams.get("variant_id"),id);
  assert.equal(env.orderFillCache.get("main:1")[0].shares,1);
  assert.equal(env.orderFillCache.get(scope+":1")[0].shares,2);
});

test("a first switch from automatic main live to Paper keeps its history regardless of fetch and paint order", async () => {
  for (const beforePaint of [true, false]) {
    const nodes=new Map();
    const node=id=>{
      if (id==="managed-execution-status") return null;
      if (!nodes.has(id)) nodes.set(id,{dataset:{},closest:()=>null,querySelector:()=>null,querySelectorAll:()=>[]});
      return nodes.get(id);
    };
    const ids={accountScopeSelect:{value:scope},openDataScope:{},orderDataScope:{},recentDataScope:{},chartTooltip:{}};
    let release, queued=false;
    const requests=[];
    const snapshot={runtime:{managed_live:{enabled:true,available:true,settings:{strategies:{
      MAIN:{enabled:true},[id]:{enabled:false}}},variants:[{variant_id:"MAIN",combo:"主账户"},{variant_id:id,combo:"拿到官方结算"}]}}};
    const ledger=[{created_at:1,total_equity:100},{created_at:101,total_equity:121}];
    const env=loadFunctions(["loadEquityCurve","parseAccountScope","handleAccountScopeChange","isExitSettleScope"],{
      document:{getElementById:node,querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){},body:{classList:{toggle(){}}}},
      window:{},setInterval(){},latestStatus:snapshot,ids,accountScope:"main",
      openDataScope:"main",orderDataScope:"main",recentDataScope:"main",handleDataScopeChange(){},
      accountScopeMeta:()=>({label:"拿到官方结算"}),isForecastPaperStrategy:()=>false,
      URLSearchParams,EQUITY_CURVE_DAYS:90,EQUITY_CURVE_MAX_POINTS:500,EQUITY_CURVE_REFRESH_MS:30000,
      equityCurveInFlight:false,equityCurvePendingForce:false,lastEquityCurveFetchMs:0,
      equityCurveRows:[],equityCurveMeta:{},lastChartRenderMs:0,
      showError:assert.fail,renderAll(){queued=true;},
      fetch:url=>{requests.push(new URL(url,"http://127.0.0.1"));return new Promise(resolve=>{
        release=()=>resolve({ok:true,json:async()=>({equity_curve:ledger,equity_curve_meta:{full_history:true}})});
      });}
    });
    vm.runInContext(script("managed-live.js"),env);
    const paint=()=>{queued=false;env.window.ManagedLiveUI.render(snapshot);};
    paint();
    assert.equal(env.window.ManagedLiveUI.active(snapshot),true);
    env.handleAccountScopeChange();
    assert.equal(env.window.ManagedLiveUI.active(snapshot),false);
    assert.equal(queued,true);
    if (beforePaint) {release();await new Promise(setImmediate);}
    paint();
    if (!beforePaint) {release();await new Promise(setImmediate);paint();}
    assert.equal(requests.length,1);
    assert.equal(requests[0].searchParams.get("full_history"),"1");
    assert.deepEqual(Array.from(env.equityCurveRows),ledger);
    assert.equal(node("equity-chart").hidden,false);
    assert.equal(node("managed-chart-empty").hidden,true);
  }
});
