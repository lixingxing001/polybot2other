const assert = require("node:assert/strict");
const {test} = require("node:test");
const {readFileSync} = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = readFileSync(path.join(__dirname, "../src/polybot2other/static/managed-live.js"), "utf8");
const id = "MAIN_FIRST_STRONG";

function dashboard(mockFetch) {
  const nodes = new Map(), events = {}, fields=new Map();
  function node(key) {
    if (key === "managed-execution-status") return null;
    if (!nodes.has(key)) nodes.set(key, {
      dataset:{}, classList:{toggle(){}}, closest:()=>null,
      querySelector:selector => node(key + " " + selector),
      querySelectorAll:selector => selector === "input[data-setting]" ? fields.get(key)||[] : [],
    });
    return nodes.get(key);
  }
  const paper = {id, label:"主账户·每局首个强信号", reason:"等待盘口稳定", snapshot:{
    account:{cash:94.98, open_positions:1}, rule:{stake_dollars:5}, equity_curve:[{at:1,equity:100}],
  }};
  const main = {runtime:{managed_live:{available:true, enabled:true, wallet:{balance:505},
    settings:{strategies:{}}, variants:[]}}};
  const env = vm.createContext({
    window:{FirstStrongPaper:paper},
    document:{getElementById:node, querySelector:()=>null, querySelectorAll:()=>[],
      addEventListener:(name,fn)=>{events[name]=fn;},body:{classList:{toggle(){}}}},
    EXIT_SETTLE_ID:"FORECAST_EXIT_SETTLE", accountScope:"main", latestStatus:main,
    parseAccountScope:s=>({variantId:s.slice("experiment:".length)}),
    isForecastPaperStrategy:()=>false, accountScopeMeta:()=>({label:paper.label}),
    loadEquityCurve:async()=>{}, renderAll(){}, setInterval(){}, showError:assert.fail,
    requestAnimationFrame:fn=>fn(), fetch:mockFetch||(()=>assert.fail("Viewing an account must not submit settings")),
  });
  vm.runInContext(script, env);
  const ui=env.window.ManagedLiveUI, controls=node("managed-strategy-controls");
  return {env,ui,controls,main,paper,events,node,fields};
}

test("an older backend presents a disabled live control and the independent Paper wallet", () => {
  const {env,ui,controls,main,paper,events,node}=dashboard();
  events.change({target:{id:"managed-data-view", value:"live"}});
  assert.equal(ui.active(main), true);
  env.accountScope="experiment:"+id;
  ui.render(main);
  assert.equal(ui.active(main), false);
  assert.equal(node("managed-view-control").hidden, true);
  assert.equal(node("managed-data-view").value, "paper");
  assert.equal(node("managed-live-tables").hidden, true);
  assert.equal(node("equity-chart-title").textContent, "Paper · 已结算净值");
  assert.match(node("managed-chart-range").textContent, /初始资金/);
  assert.match(controls.innerHTML, /type="checkbox" disabled/);
  assert.match(controls.innerHTML, /等待服务器提供实盘控制/);
  assert.match(controls.innerHTML, /data-first-strong-account/);
  assert.doesNotMatch(controls.innerHTML, /data-live-toggle|data-live-save/);
  const card=controls.querySelector("[data-first-strong-card]");
  assert.equal(card.querySelector("[data-paper-balance]").textContent,"模拟可用资金 $94.98");
  assert.match(card.querySelector("[data-paper-budget]").textContent,/\$5\.00（含费） · 1 笔持仓/);
  assert.equal(node("managed-wallet-balance").textContent,"$505.000000");
  paper.snapshot=null;
  paper.reason="正在连接模拟账户";
  ui.render(main);
  assert.equal(card.querySelector("[data-paper-balance]").textContent,"模拟可用资金 —");
  assert.match(card.querySelector("[data-paper-reason]").textContent,/正在连接模拟账户/);
  assert.equal(node("managed-chart-empty").hidden,false);
  env.accountScope="main";
  assert.equal(ui.active(main),true);
});

test("the new control saves only this strategy, separates Paper budget and scopes real history",async()=>{
  const posted=[];
  const {env,ui,controls,main,paper,events,node,fields}=dashboard(async(url,options)=>{
    if(url==="/api/managed-live")return {ok:true,json:async()=>({control_token:"fixture"})};
    assert.equal(url,"/api/managed-live/settings");
    const payload=JSON.parse(options.body);
    posted.push(payload);
    Object.assign(root.settings.strategies[id],payload.strategies[id]);
    root.settings.revision++;
    return {ok:true,json:async()=>root};
  });
  const root=main.runtime.managed_live;
  const original={enabled:true,stake_dollars:11,max_open_trades:2,max_daily_loss:20};
  const config={enabled:false,stake_dollars:4,max_open_trades:2,max_daily_loss:15};
  root.settings={revision:12,strategies:{MAIN:{...original},[id]:config}};
  root.variants=[{variant_id:"MAIN",settled_pnl_curve:[{created_at:10,realized_pnl:88}]},
    {variant_id:id,combo:paper.label,settled_pnl_curve:[],metrics:{realized_pnl:0}}];
  root.wallet.total_equity=505;
  const selector='[data-live-card="'+id+'"]';
  const liveCard=controls.querySelector(selector);
  const inputs=Object.keys(config).filter(key=>key!=="enabled").map(key=>({dataset:{setting:key}}));
  fields.set("managed-strategy-controls "+selector,inputs);
  const scope="experiment:"+id;
  env.accountScope=scope;
  ui.render(main);
  assert.equal((controls.innerHTML.match(/data-first-strong-card/g)||[]).length,1);
  assert.equal((controls.innerHTML.match(/data-live-toggle="MAIN_FIRST_STRONG"/g)||[]).length,1);
  assert.match(controls.innerHTML,/data-live-toggle="MAIN"/);
  assert.match(controls.innerHTML,/每笔金额（仅实盘，含费）/);
  assert.match(controls.innerHTML,/风险设置（仅实盘）/);
  assert.match(controls.innerHTML,/max="2" step="1" data-setting="max_open_trades" aria-label="主账户·每局首个强信号最多持仓"/);
  assert.match(controls.innerHTML,/max="10" step="1" data-setting="max_open_trades" aria-label="主账户最多持仓"/);
  assert.equal(node("managed-view-control").hidden,false);
  assert.equal(liveCard.querySelector("input[data-live-toggle]").checked,false);
  assert.equal(inputs[0].value,4);
  assert.equal(ui.active(main),false);
  events.change({target:{id:"managed-data-view", value:"live"}});
  ui.render(main);
  assert.equal(ui.active(main),true);
  assert.equal(node("managed-live-tables").hidden,false);
  assert.equal(node("managed-chart-empty").hidden,false);
  assert.equal(ui.curve(main).length,0);
  assert.equal(ui.currentPoint(main),null);
  assert.equal(posted.length,0);
  root.variants[1].settled_pnl_curve.push({created_at:20,realized_pnl:-3});
  ui.render(main);
  assert.deepEqual(Array.from(ui.curve(main),p=>p.total_equity),[-3]);
  assert.equal(node("managed-chart-empty").hidden,true);
  events.change({target:{id:"managed-data-view",value:"auto"}});
  inputs[0].value="7";
  events.change({target:{checked:true,dataset:{liveToggle:id}}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(posted,[{strategies:{[id]:{stake_dollars:7,max_open_trades:2,max_daily_loss:15,enabled:true}}}]);
  assert.equal(ui.active(main),true);
  assert.equal(paper.snapshot.rule.stake_dollars,5);
  assert.deepEqual(root.settings.strategies.MAIN,original);
  events.change({target:{checked:false,dataset:{liveToggle:id}}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(posted[1],{strategies:{[id]:{enabled:false}}});
  assert.equal(ui.active(main),false);
  assert.equal(root.enabled,true);
});
