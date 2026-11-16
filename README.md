# EdgePulse

The dashboard has five isolated Paper accounts: **主账户** (`MAIN`), **主账户（优化版）** (`MAIN_OPTIMIZED`), **主账户自适应成长版** (`MAIN_ADAPTIVE`), **主账户（优化极速赚钱版）** (`MAIN_OPTIMIZED_FAST`), and **主账户（优化进阶版）** (`MAIN_OPTIMIZED_PLUS`). The adaptive account starts from settled samples in the optimized ledger and keeps its own balance and bounded score calibration. FAST and PLUS each start with a separate $100 Paper account and do not copy historical profits. In the default managed-live runtime, all five share the verified PolyBolt spot/TWAP and CLOB collector used by live execution. Existing strategies' entry rules and retained ledgers are unchanged.

The dashboard also provides opt-in real execution for these five strategies. The global live switch and the corresponding strategy switch must both be enabled before new real BUY orders are allowed; all switches default off in a new live ledger. Adding FAST or PLUS leaves saved switches intact and adds the new live switch as OFF. Each strategy has its own fee-inclusive order budget and accounting, while the actual wallet balance is shared. Turning either switch off stops new entries and continues managing existing positions, including historical positions from retired strategies. The adaptive live strategy uses the bounded calibration model built from the optimized and adaptive Paper ledgers; if that model cannot be opened, its entry state is shown as unavailable and it cannot silently fall back to the optimized strategy.

Each strategy's risk settings include **重置亏损限额** for its live account. Clicking it keeps the saved limit and starts its loss accounting from that moment, within the existing rolling 24-hour window. The reset survives restarts, preserves historical P&L and Paper accounting, and retains the risk of existing holdings and uncertain orders. Later settlements count from the saved realized-P&L baseline, including positions partially sold before the reset.

The top metrics and tables follow the selected strategy's effective Paper/live mode by default. A separate data-view selector can inspect either ledger without enabling trading. Wallet cash comes from authenticated CLOB balance queries, and portfolio value comes from the official Data API; failed reads are shown as unavailable or stale rather than a fabricated zero. Live strategy P&L is based on confirmed official fills and the market's fee schedule. Resolved winning shares are shown as pending redemption and are never credited to the displayed cash balance before the wallet API reports the cash.

Live execution uses its own validated TWAP reference and fresh order books. It does not copy Paper fills. Signed order hashes are saved before a single POST; uncertain results retain reservations and are reconciled rather than blindly resubmitted. Partial fills retain remaining shares. Settings and manual exits are restricted to same-origin loopback requests with a per-process control token. The live ledger and public reference cache are under `data/live-accounts/`; credentials remain only in the existing owner-readable environment file. No blockchain approvals, funding transfers, or redemptions are performed automatically.

**主账户止盈止损** (`MAIN_TP_SL`) and **价值确认 V1** (`VALUE_CONFIRM_V1`) were retired on 2026-11-14. They have no active Paper registration or live entry controls. Old launcher selections are ignored, saved retirement settings are kept only for historical exits, and direct new BUY requests are rejected. Their historical databases are retained without being started. Existing confirmed positions and uncertain orders remain eligible for reconciliation and settlement.

Read the optimized-account specification at `http://127.0.0.1:8791/static/optimized-main.html`. Its initial virtual equity is aligned with the original main account at creation; subsequent restarts neither re-fund it nor reset comparison history. The dashboard's same-period comparison excludes original historical profit and carryover positions, distinguishes pending exposure, and reports actual P&L/percentage-point differences only after trades exist. Model changes and the early mid-price filter are forward-testing hypotheses, not proven profit improvements.

The packaged [historical review](src/polybot2other/static/strategy-review.html) remains available. Detailed loss, exit and trading-hour analyses are generated locally under `reports/`. Their Python analysis/rendering scripts are versioned; database snapshots, generated reports, audit manifests and local configuration are excluded from Git. These research scripts require the corresponding local snapshots and backup metadata. Retired strategy sources and ledgers are archived under `data/backups/strategy-retirement-20261026-225605/`. Live operation remains opt-in through the new dashboard switches.

The reference notes below also document legacy features retained for historical compatibility; they do not re-enable retired strategies.

Real Polymarket BTC 5-minute Up/Down paper and live-trading dashboard.

The runtime package and existing data paths still use `polybot2other` for command and database compatibility.

The default mode still runs Paper trading continuously. Live trading is opt-in and disabled until the dashboard setting is explicitly enabled:

- Initial balance defaults to `$100.00`.
- Only BTC 5-minute markets are tracked.
- Current market discovery uses Polymarket Gamma event slugs such as `btc-updown-5m-<window_start_unix>`.
- Orderbook quotes use Polymarket CLOB token ids for the active Up/Down market.
- The backend maintains the active market orderbook through Polymarket CLOB market WebSocket and falls back to CLOB REST when the backend WebSocket quote feed is stale.
- The browser dashboard still uses WebSocket streams for display and analysis, but browser feeds do not drive Paper or live order decisions.
- Paper entries are simulated as CLOB-style orders. The default entry mode is FAK, walks visible ask levels up to the execution limit, and charges the configured taker fee per filled level.
- GTC/GTD/POST_ONLY are simulated as resting maker orders. GTC can rest until market end, GTD expires after `POLYBOT2OTHER_PAPER_GTD_SECONDS`, and POST_ONLY additionally requires queue time, price-through-limit movement, and partial queue fills.
- Resting maker partial fills from the same paper order are aggregated into one open position. Tiny resting fills below `$0.01` are ignored, and residual reserved cash at or below `$0.05` is released as dust instead of creating `$0.00` open positions.
- Paper order attempts and per-level paper fills are stored separately from positions for execution-quality review.
- The current Paper configuration contains the original main account and three isolated experiments. `docs/strategy-combinations-guide.html` describes legacy combinations retained for historical reference.
- `SINGLE_FAK_REAL` uses a separate live SQLite database and follows the current `SINGLE_FAK` paper strategy behavior: FAK taker entry and legacy same-market opposite-side reversal allowed after any pending live buy for that market has settled. `SINGLE_FAK_AGGRESSIVE_EDGE_REAL` uses its own live SQLite database and applies the same Aggressive Edge filter as the `SINGLE_FAK_AGGRESSIVE_EDGE` Paper strategy before the live FAK entry path.
- Live trading uses a configured soft budget cap (`initial_balance`) instead of trusting the full wallet balance. This is a software guard, not an on-chain limit.
- If `stake_dollars` is changed while the current market already has a live position, that current market keeps using the original position stake for any same-market reversal leg. The new configured stake applies to the next market.
- Before live entry, the bot checks Polymarket CLOB collateral/pUSD `balance` and `allowance`; both must cover the actual stake for the next order.
- Before live entry, the bot checks Polymarket's geoblock endpoint and blocks live arming/new BUY orders when the current runtime region is reported as restricted. This does not bypass Polymarket access restrictions.
- Live BUY orders pass the configured stake as the SDK field named `user_usdc_balance`; in CLOB V2 the wallet asset to fund/approve is Polymarket collateral/pUSD. SDK fee adjustment cannot sign an order whose total cost exceeds the bot's per-order software budget.
- Live FAK orders pass the market `tick_size` and `neg_risk` flags from the active CLOB quote. When the CLOB response includes matched `makingAmount` / `takingAmount`, local records use those official matched amounts before falling back to the pre-order orderbook sweep estimate.
- Live orders that return an order id but no confirmed fill are recorded as `PENDING(待官方确认)`, reserve the configured stake in the software account, and are rechecked through official order/trade queries until they become filled or no-fill.
- When a live BUY returns an official order id, the bot records a local `PENDING(待官方确认)` order before any extra amount reconciliation. If local accounting fails after the official order may have reached CLOB, live trading is automatically disabled to prevent repeating the same real signal.
- While a live buy order is `PENDING(待官方确认)`, the same market is blocked from submitting another live buy in either direction. This prevents a reversed signal from sending an opposite-side order before the first CLOB status is known.
- Manual live sell only closes local holdings after the CLOB response confirms a fill. Partial sell fills close only the matched shares; the remaining shares stay open.
- When a manual live sell returns an official order id, the bot records a local `PENDING(待官方确认)` exit order before closing shares. If local accounting fails after the official sell may have reached CLOB, live trading is automatically disabled and the trade is blocked from duplicate manual sell by the pending exit order.
- Before manual live sell, the bot checks the conditional token `balance` and `allowance` for the outcome token; both must cover the shares being sold.
- The live switch uses a process lock next to the live settings file. Only one service process can hold the same `live-settings.json.lock` and submit real orders for that live account.
- `POLYBOT2OTHER_LIVE_TRADING_RUNTIME_ENABLED=false` is a process-level hard kill switch: the service will not create the live runner, so live settings changes, live preflight, live sell, and live emergency stop cannot place or manage real orders from that process.
- Trades, settlements, and equity curve are stored in SQLite.
- Recent trades include a trade-level settlement source. `polymarket_official` means the market winner came from Polymarket Gamma resolved prices; `chainlink_fallback` means the bot used the local Chainlink price against the target because the official winner was not available yet; `early_exit` means the position was closed before market settlement. The bot periodically rechecks fallback settlements and upgrades or corrects them when the official outcome appears. When Polymarket exposes `finalPrice` / `priceToBeat` in event metadata, the bot records those official settlement prices for final price and final distance display; if metadata is missing, it falls back to a one-time Polymarket page payload parse after settlement.

## Run

```bash
rtk proxy python3 -m venv .venv
rtk proxy .venv/bin/python -m pip install -e .
rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.web --host 127.0.0.1 --port 8787
```

On macOS, keep an imported Linux virtual environment intact and install a native runtime separately:

```bash
uv venv --python 3.12 .venv/macos
uv pip install --python .venv/macos/bin/python -e . 'py_clob_client_v2==1.0.1'
./start-macos.sh
# Stop the Mac process when needed:
./stop-macos.sh
```

The Mac launcher runs in the background at `http://127.0.0.1:8791` and reuses the existing `data/` databases and settings. It enables the managed-live feature while leaving its initial trading switches off; legacy live runners are not instantiated in managed mode. `POLYBOT2OTHER_MANAGED_LIVE_ENABLED=true` selects this interface, and `POLYBOT2OTHER_LIVE_TRADING_RUNTIME_ENABLED=false` remains a process-level prohibition on real orders. Logs are written to `logs/polybot2other-macos-8791.log`. Existing BTC and Market Scout pause settings remain in effect.

In managed mode, Paper and live inputs use `SharedMarketInputs`: the same verified market/opening, Chainlink spot, TWAP reference history, source timestamps, auxiliary basis data and directional books. Browser snapshots and legacy REST/RTDS Chainlink values cannot overwrite them. Turning live trading off does not turn this collector off; missing verified data produces a waiting state rather than a fallback to the legacy opening. A status response uses one captured frame for both Paper and live views. Pausing an independent Paper consumer does not reset or close the shared collector. Aligned Paper settlement also waits for the official outcome instead of provisionally using a later spot price. Explicitly disabling the managed feature retains the standalone legacy compatibility path.

This is an input-layer change. Strategy formulas, per-account settings, fee models, Paper fill simulation, entry limits and exit-policy thresholds are retained. Account state, configured fees, real liquidity and submission checks can still produce different executions; identical market input does not guarantee identical fills or profits. Existing ledgers are not recalculated, and pre-alignment Paper results use the old data convention.

On macOS the launcher also enables `POLYBOT2OTHER_KEEP_AWAKE=true`. A process-scoped `caffeinate -i -s -w <service PID>` guard prevents idle sleep while the service is running and is released on shutdown; display sleep remains allowed. Keep the laptop lid open, preferably on power: ordinary lid-close/manual sleep is not overridden or promised to keep trading alive. No permanent power settings or login auto-start are installed. To opt out, run `POLYBOT2OTHER_KEEP_AWAKE=false ./start-macos.sh`. A detected runtime interruption/clock discontinuity reconnects the live feeds and requires new wallet, region and open-order checks; a response begun before the interruption is not treated as a fresh balance. No order is resubmitted by recovery.

Managed-live books use a buffered WebSocket transport that retains handshake-tail bytes and incomplete frames/messages across read timeouts. Complete depth is kept internally and ordered deltas are consumed before publishing bounded views. Stalled/backlogged connections resynchronize; a separate read-only worker repairs ageing sides using the official `/book` endpoint. REST repair preserves the server and receive timestamps and still requires the original 3-second source / 2.5-second receive freshness limits. An old response cannot overwrite a newer quote or another round. Aligned Paper consumers use these same books without a second authoritative subscription. The dashboard reports the power guard, quote ages, reconnections and repairs; these indicate data availability, not a guarantee that a trading signal will qualify.

Managed-live P&L uses a separate, read-only valuation worker. It retains the last valid order book across brief outages, round changes and restarts, and recalculates remaining shares with depth and fees. The dashboard labels cached estimates with their age or pending settlement status; no quote is represented as zero profit. Display refresh may accept a dated book up to 30 seconds old; execution retains its strict 3-second source and 2.5-second streaming-receipt checks. Cached display values never trigger orders or credit the cash balance.

Managed-live order reconciliation skips an in-flight POST and retries reads without blindly resubmitting an uncertain order. If an order remains unresolved for 60 seconds and authenticated wallet/open-order snapshots are fresh, it enters `QUARANTINED`: its BUY budget stays reserved, its possible position and remaining cost count against the existing position/daily-loss limits, and the original market remains blocked across all strategies. Other markets may use the remaining limits. Partial fills do not double-count a position or its budget. Quarantined orders are checked every 15 seconds, survive restarts, and stay visible beyond the latest 50 orders. The adapter cross-checks trades, open orders and token holdings even when lookup by ID is empty; empty results or timeouts alone never release money. Only verified terminal evidence (including all final receipts identified by a saved successful FAK response) releases the uncertainty. Known explicit exchange rejections are recorded separately, with sanitized submission metadata retained. Identity/accounting failures remain durable stops across restarts. The dashboard shows each strategy's entry state, reason, duration, retry policy and pause/recovery/isolation events; a missing dashboard heartbeat is shown as unknown live status.

For a quarantined order with no exchange acknowledgement and no confirmed fills, an additional `EXPIRED_UNFILLED` outcome closes long-lived missing records. It requires a market that ended at least 10 minutes ago and has an official resolution, a missing order lookup, successful complete trade/open-order queries with no matching activity, a fully paginated independent wallet-activity query with no relevant records, and verified token balances (zero for a BUY; all locally owned shares still present for a SELL). Wallet and open-order snapshots must remain fresh. Three consecutive clean checks spanning at least 30 seconds are required; any missing/conflicting check resets them. The final evidence is retained, no fill or cash is invented, and affected SELL holdings then settle using the official outcome. Finalized records continue low-frequency read-only audits (one due record per reconciliation pass, at least five minutes apart); contradictory late order/trade evidence restores the reservation and raises a durable review alert. Raw request/signature data is never stored; text, JSON and nested API errors retain a redacted error message for diagnosis.

Wallet activity alone is not authenticated evidence that a specific order filled. Late activity without an order/trade match isolates only that order and retains its bounded budget; it does not create a wallet-wide integrity fault. For a finalized SELL, a later `REDEEM` is recognized as settlement only when the saved finalization proved those holdings intact, the token/timing/payout match, and the total burned shares do not exceed the proved holdings. Pagination continues after such rows so that a later-page trade cannot be missed. This exception does not apply to BUY orders or initial finalization checks. Earlier wallet-activity-only false alarms are automatically rechecked: normal settlement restores the prior final state, while ambiguous activity keeps the order isolated. Original fault evidence and the recovery proof are retained in `audit_recovery_json`; authenticated late fills, identity mismatches and budget faults remain subject to reconciliation review.

The service automatically reads local `.env.live`, `.env.local`, and `.env` files for `POLYBOT2OTHER_*` keys. Existing process environment variables always win. To use a different file, set `POLYBOT2OTHER_ENV_FILE=/path/to/file`.
Live readiness reports env file paths, geoblock status, and credential presence booleans only; it does not return private key, API secret, passphrase, or public IP values.
Blank values in env files are recorded as empty and are not loaded into the process environment. This keeps the local `.env.live` template from masking real values supplied by `.env.local`, `.env`, or the process environment.
When a loaded env file contains live private key or API credential fields, readiness also checks that the file is owner-only, for example `chmod 600 .env.live`; loose permissions block live trading before any real order can be attempted.
Readiness also reports the installed `py_clob_client_v2` version and validates the exports plus SDK methods used by the live path, so an incompatible SDK version blocks live enable before any real order is attempted.
If live credential environment values change while the service is running, the SDK client and balance/open-order caches are rebuilt instead of reusing the previous wallet state.

To create or update `.env.live` without exposing secrets in this chat, use the local interactive setup tool from this machine:

```bash
rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_env_setup --service-url http://127.0.0.1:8791
```

It prompts in the terminal, writes only local `.env.live`, sets `chmod 600`, optionally reloads the running dashboard service, and prints only masked/boolean credential status.

Open:

```text
http://127.0.0.1:8787
```

## Test

```bash
rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

## Retrospective Snapshot

```bash
rtk proxy env PYTHONPATH=src python3 -m polybot2other.report_snapshot --output docs/strategy-experiments-retrospective-latest.html
```

## Live Preflight CLI

```bash
rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --pretty
rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --pretty --require-arming-ready
rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --service-url http://127.0.0.1:8791 --pretty --require-arming-ready
rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_doctor --pretty --require-one-shot-ready
rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_doctor --service-url http://127.0.0.1:8791 --pretty --require-one-shot-ready
rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_env_setup --service-url http://127.0.0.1:8791
```

The command prints the same read-only live preflight payload used by `/api/live-preflight`. `ready=true` means the live switch is already on and the next eligible signal can place a real order. `arming_ready=true` means every check except the live switch itself is passing, so the account is ready to be armed from the dashboard.
By default the CLI prints only `live_preflight` to keep terminal output small; use `--include-snapshot` when you need the full dashboard snapshot for debugging.
`live_doctor` additionally returns a ready-to-run one-shot command. Its `--max-stake` value is generated from the current live preflight stake, including the locked stake for a same-market reversal leg, so prefer the doctor output over copying a stale fixed amount.
Use `--service-url` for the final preflight and doctor checks when the dashboard service is already running; that reads the same in-process market snapshot, live settings, and browser/REST-fed quote state that would be used by the dashboard.
If the current market reports `min_order_size` above the configured `stake_dollars`, doctor returns a `min_order_size` fatal blocker plus `first_order.stake_requirement`. Raise `stake_dollars` to at least `suggested_stake_dollars`, save settings, and run doctor again before attempting one-shot.
The dashboard `执行首单` button uses the same `/api/live-once` path. It stays disabled until a fresh `首单检查` has no fatal blockers, asks you to type `PLACE_REAL_ORDER`, then posts the doctor-recommended max-stake body and renders the returned blocker/order/evidence fields.

## One-Shot Live Order CLI

```bash
rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_once \
  --service-url http://127.0.0.1:8791 \
  --confirm-real-order \
  --acknowledge-compliance \
  --max-stake 2 \
  --wait-ready-seconds 180 \
  --wait-reconcile-seconds 20 \
  --require-submitted \
  --pretty
```

This command may submit one real `SINGLE_FAK_REAL` BUY order when the live switch is currently off and the current market, signal, wallet balance/allowance, geoblock check, official open orders, process lock, and risk checks all pass. For the first order, prefer `--service-url` so the command posts to the running dashboard process and uses its in-memory market/quote snapshot. Without `--service-url`, the CLI creates a fresh local bot process and relies on REST fallback data. It requires `--confirm-real-order` plus a `--max-stake` cap, and defaults to disabling live trading again immediately after the one-shot run. `--wait-ready-seconds` can wait through transient blockers such as a just-rotated market missing `market.target_price`, a temporary `NO_TRADE` signal, or thin orderbook depth; it fails fast for credentials, compliance, wallet, geoblock, software cash, official open orders, or other non-transient blockers. `--wait-reconcile-seconds` polls official order/trade reconciliation for a short window before the one-shot process releases the live process lock, and includes the local order status in the output. Successful output includes a read-only `evidence` package by default, so the same response shows the local live ledger row, software account, official open orders, and wallet/readiness state after the attempt; pass `--no-evidence` only when you intentionally want smaller output. When an order attempt is submitted or has an order id, the service also writes a sanitized JSON audit artifact under `data/live/audit/` and returns its path in `live_once.audit`. Use `--leave-enabled` only when you intentionally want the normal dashboard loop to keep trading after the first attempt.

## Live Evidence CLI

```bash
rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_evidence \
  --service-url http://127.0.0.1:8791 \
  --external-order-id live-order-id \
  --pretty
```

The evidence command is read-only. Prefer `--service-url` after the first order so the evidence package is collected from the same dashboard process that submitted the order. It collects the local `SINGLE_FAK_REAL` ledger, software-account metrics, readiness/wallet state, official CLOB open orders, open live trades, recent live orders/trades, pending orders, and the local row mapped from an official order id. The output intentionally omits local `raw_response`, private keys, API secrets, and signed order payloads.

## Runtime Files

```text
data/polybot2other-real-btc.sqlite3
data/live/single_fak_real.sqlite3
data/live/live-settings.json
data/live/audit/live-once-*.json
```

## API

```text
GET /api/recent-trades?limit=100&offset=0&start_at=1779870000&end_at=1779873600
GET /api/recent-trades?account_scope=live&limit=100&offset=0
GET /api/recent-trades?account_scope=strategy_experiment&variant_id=SINGLE_FAK&limit=100&offset=0
GET /api/orders?limit=20&offset=0&status=all
GET /api/orders?account_scope=live&limit=20&offset=0&status=all
GET /api/order-fills?order_id=1&account_scope=live
GET /api/equity-curve?account_scope=main&days=90&max_points=1200
GET /api/equity-curve?account_scope=strategy_experiment&variant_id=SINGLE_FAK&days=90&max_points=1200
GET /api/equity-curve?account_scope=live&variant_id=SINGLE_FAK_REAL&days=90&max_points=1200
GET /api/live-settings
GET /api/live-preflight?include_snapshot=false
GET /api/live-open-orders
GET /api/live-evidence?external_order_id=live-order-id&force=true&include_snapshot=false
GET /api/live-doctor?refresh=true&include_snapshot=false
GET /api/polymarket-status?refresh=true
POST /api/live-once
GET /api/strategy-experiments
GET /api/strategy-experiments?variant_id=SINGLE_FAK&trade_limit=50&order_limit=50
GET /api/strategy-experiments-retrospective?start_at=1779870000&end_at=1779873600
GET /api/strategy-experiments-tables?trade_limit=100&order_limit=20&status=all
GET /strategy-experiments-retrospective.html?start_at=1779870000&end_at=1779873600
POST /api/cancel-order {"order_id":1}
POST /api/cancel-orders {"scope":"current_market"}
POST /api/cancel-orders {"scope":"all"}
POST /api/live-settings {"enabled":false,"initial_balance":20,"stake_dollars":2,"max_open_trades":2,"max_entry_price":0.72,"max_daily_loss":6,"max_total_drawdown":12,"retry_count":2,"retry_delay_ms":250,"compliance_acknowledged":true}
POST /api/live-reload-credentials
POST /api/live-toggle {"enabled":true}
POST /api/live-preflight
POST /api/live-once {"confirm":"PLACE_REAL_ORDER","acknowledge_compliance":true,"max_stake_dollars":2,"disable_after":true,"wait_ready_seconds":180,"ready_poll_seconds":2,"reconcile_wait_seconds":20,"include_evidence":true}
POST /api/live-emergency-stop
POST /api/live-sell {"trade_id":1}
```

`/api/orders` status filters: `all`, `active`, `filled`, `canceled`, `expired`, `rejected`. `active` includes live `PENDING` orders, paper resting orders, and partial resting orders.

`/api/live-preflight` is read-only. If market, signal, wallet, and risk checks pass, it also constructs and signs the current FAK order through the SDK without calling `post_order` or exposing the signed payload.

`/api/live-preflight` and the CLI include `blocked_checks`, `ready`, `arming_ready`, `can_enable_live`, and `can_place_next_order`. Use `arming_ready` before turning on the live switch; use `ready` and `can_place_next_order` after the switch is already on.

`/api/live-once` is the controlled first-order path. It requires the normal live switch to be off before starting, the confirmation string `PLACE_REAL_ORDER`, and a `max_stake_dollars` cap. It may submit one real order when checks pass and defaults to disabling live trading after that one run. Add `reconcile_wait_seconds` when you want the response to poll official order/trade state briefly after submission. If the one-shot run is blocked before submission, the response includes `live_once.blocked=true`, `blocked_keys`, `fatal_blocked_keys`, `waitable_blocked_keys`, and the latest `preflight` payload so the exact blocker can be fixed without guessing. Successful responses include the sanitized evidence package by default; set `include_evidence=false` only when you intentionally want smaller output. Submitted one-shot attempts also create a sanitized local audit JSON under `data/live/audit/`; audit write failure is reported in `live_once.audit` but does not hide the order result.

`/api/live-open-orders` is read-only and returns the official CLOB open orders visible to the configured SDK account. Normal dashboard polling uses a short cache; the endpoint and `刷新挂单` button force a fresh read. A nonzero official open order count blocks live arming, preflight readiness, and new live BUY submissions until the official account is clear. When credentials are missing, the read is skipped instead of repeatedly attempting official API authentication.

`/api/live-evidence` is read-only and is intended for first-order verification or incident review. Pass `external_order_id` after a real order attempt to see whether that official id exists in the local live ledger, whether the local status is still `PENDING(待官方确认)` or already filled/canceled/rejected, and whether official open orders remain. Official `ORDER_STATUS_INVALID` / rejected / failed / error states are recorded locally as `REJECTED(已拒绝)`; canceled, expired, and unmatched no-fill states are recorded as `CANCELED(已取消)`. It defaults to `include_snapshot=false` to keep first-order evidence responses small. It does not submit, cancel, or sell orders.

`/api/live-doctor` is read-only and compresses live settings plus preflight into a first-order checklist: one-shot readiness, SDK compatibility, fatal blockers, transient blockers, credential setup status, next actions, recommended one-shot command, and post-order evidence checklist.

`/api/live-doctor` also returns `first_order.stake_requirement` with the configured stake, current market minimum order size, shortfall, and a recommended settings patch when the stake is below the official minimum. The bot will not submit a real order while `stake_dollars < min_order_size`.

`/api/polymarket-status` is a read-only backend proxy for Polymarket's official status summary. It caches successful responses briefly, returns stale cached data with an error field when the official status page cannot be reached, and is intended for dashboard visibility only. It does not replace live preflight, wallet, market, orderbook, or official open-order checks.

`/api/live-reload-credentials` reloads only live credential env keys from `.env.live` / `.env.local` / `.env` or `POLYBOT2OTHER_ENV_FILE` in the running dashboard process, then clears cached SDK authentication state. Use it after editing private key, signature type, funder, or CLOB API credential values while the service is already running. It does not submit, cancel, or sell orders. Non-credential settings such as database path and default risk config still require a normal restart or the existing settings UI/API.

Manual `/api/live-sell` orders follow the same official reconciliation rule as live buys: if Polymarket returns an order id but no confirmed fill, the local exit order is kept as `PENDING(待官方确认)` and the live position stays open until official order/trade recheck proves a fill or no-fill terminal status.

While a live exit order is `PENDING(待官方确认)`, the server rejects another `/api/live-sell` for the same trade and the dashboard shows `卖出确认中`. This prevents duplicate SELL submissions for the same conditional token shares during network or CLOB status uncertainty.

The live runner is guarded by a non-blocking in-process mutex. If a background tick and manual sync overlap, the second live run is skipped and `overlap_skip_count` increments, so the same signal cannot submit duplicate real orders concurrently.

Immediately before submitting a live BUY to the official CLOB SDK, the runner rechecks that `enabled=true`, risk confirmation is still present, and the official CLOB open order count is still zero. If the switch was turned off or another official open order appeared while earlier checks were running, the order is not submitted.

When live trading is enabled, the process must hold `data/live/live-settings.json.lock`. A second service process pointing at the same live settings path will keep `enabled=false` and show a process-lock blocking reason. Manual live sell also takes this lock if the live switch is off, so two processes cannot submit duplicate SELL requests for the same live account.

## Environment

```text
POLYBOT2OTHER_INITIAL_BALANCE=100
POLYBOT2OTHER_DB_PATH=data/polybot2other-real-btc.sqlite3
POLYBOT2OTHER_TICK_SECONDS=2
POLYBOT2OTHER_STAKE_DOLLARS=5
POLYBOT2OTHER_MAX_OPEN_TRADES=2
POLYBOT2OTHER_MIN_CONFIDENCE=0.62
POLYBOT2OTHER_MAX_ENTRY_PRICE=0.72
POLYBOT2OTHER_PAPER_ENTRY_ORDER_TYPE=FAK
POLYBOT2OTHER_PAPER_TAKER_FEE_RATE=0.07
POLYBOT2OTHER_PAPER_GTD_SECONDS=90
POLYBOT2OTHER_STRATEGY_EXPERIMENTS_ENABLED=true
POLYBOT2OTHER_STRATEGY_EXPERIMENTS_DB_DIR=data/strategy-experiments
POLYBOT2OTHER_STRATEGY_EXPERIMENTS_VARIANTS=
POLYBOT2OTHER_LLM_SUPER_AGENT_ENABLED=true
POLYBOT2OTHER_LLM_API_KEY=
POLYBOT2OTHER_LLM_BASE_URL=https://api.hao.ai/v1
POLYBOT2OTHER_LLM_MODEL=openai/gpt-5.4-mini
POLYBOT2OTHER_LLM_TIMEOUT_SECONDS=1.2
POLYBOT2OTHER_LLM_MIN_INTERVAL_SECONDS=12
POLYBOT2OTHER_LIVE_TRADING_DB_PATH=data/live/single_fak_real.sqlite3
POLYBOT2OTHER_LIVE_TRADING_SETTINGS_PATH=data/live/live-settings.json
POLYBOT2OTHER_LIVE_CHAIN_ID=137
POLYBOT2OTHER_LIVE_TRADING_RUNTIME_ENABLED=true
POLYBOT2OTHER_LIVE_DEFAULT_INITIAL_BALANCE=20
POLYBOT2OTHER_LIVE_DEFAULT_STAKE_DOLLARS=2
POLYBOT2OTHER_LIVE_DEFAULT_MAX_DAILY_LOSS=6
POLYBOT2OTHER_LIVE_DEFAULT_MAX_TOTAL_DRAWDOWN=12
POLYBOT2OTHER_LIVE_DEFAULT_RETRY_COUNT=2
POLYBOT2OTHER_LIVE_DEFAULT_RETRY_DELAY_MS=250
POLYBOT2OTHER_LIVE_PRIVATE_KEY=
POLYBOT2OTHER_LIVE_SIGNATURE_TYPE=
POLYBOT2OTHER_LIVE_FUNDER_ADDRESS=
POLYBOT2OTHER_LIVE_API_KEY=
POLYBOT2OTHER_LIVE_API_SECRET=
POLYBOT2OTHER_LIVE_API_PASSPHRASE=
POLYBOT2OTHER_GAMMA_URL=https://gamma-api.polymarket.com
POLYBOT2OTHER_CLOB_URL=https://clob.polymarket.com
```

`POLYBOT2OTHER_LIVE_SIGNATURE_TYPE` must be one of Polymarket's signature types: `0` EOA, `1` proxy wallet, `2` Gnosis Safe, or `3` deposit wallet. `POLYBOT2OTHER_LIVE_FUNDER_ADDRESS` is the address that holds the trading collateral/pUSD and allowances. For `signature_type=0`, readiness now verifies that the funder address equals the private-key signer address; for proxy, Safe, and deposit-wallet modes, readiness shows both addresses so they can be checked before trading. For new `signature_type=3` deposit-wallet users, fund and approve the deposit wallet itself, not only the owner EOA. `POLYBOT2OTHER_LIVE_API_KEY`, `POLYBOT2OTHER_LIVE_API_SECRET`, and `POLYBOT2OTHER_LIVE_API_PASSPHRASE` must be provided together; if all three are left empty, the SDK path attempts to derive API credentials from the private key.

`POLYBOT2OTHER_LIVE_DEFAULT_RETRY_COUNT` and `POLYBOT2OTHER_LIVE_DEFAULT_RETRY_DELAY_MS` are also editable from the dashboard. They cover live order create/sign, order post, wallet/token balance and allowance sync, and official order/trade rechecks for retryable timeout/network/429/5xx failures.

Set `POLYBOT2OTHER_LIVE_TRADING_RUNTIME_ENABLED=false` when you want a process to run Paper collection only. This is stronger than turning off the dashboard live switch because the live runner is not instantiated at all.

LLM routing is no longer part of strategy experiments. If enabled for the main bot, it remains a non-blocking router guarded by deterministic risk checks.

For the end-to-end live setup checklist, use `docs/live-trading-runbook.md`. Copy `.env.live.example` to `.env.live`, run `chmod 600 .env.live`, then fill the private values locally. `.env.live` is ignored and must hold the real private values only on the local machine.

For safety, a saved `enabled=true` live switch is not preserved across process restarts. On startup the service rewrites live settings to `enabled=false` and shows that manual preflight/re-enable is required before any new real order can be submitted.

## Paper execution aligned with managed live (2026-11-06)

The Mac launcher enables `POLYBOT2OTHER_PAPER_REALISTIC_EXECUTION=true`. The retained
Paper accounts reuse the existing `LiveSignals` functions, verified market inputs,
and the saved per-strategy live budgets, position limits, daily-loss limits and
TP/SL percentages. Live enable switches still control real orders only; the Paper
pause button controls simulation. A settings change affects subsequent submissions,
not the budget already reserved by an in-flight order. Each strategy tests a separate
$100 virtual wallet; this is a standalone strategy comparison, not a simulation of
four strategies competing for one shared live wallet.

`paper_execution.py` accepts only read-only metadata/book/configuration callbacks.
It cannot sign, post, cancel or redeem a real order. The entry limit, fee reserve,
minimum size and V2 amount precision mirror the current managed-live adapter. It
uses the market's current fee curve, not a fixed guessed fee. The TP/SL strategy
uses its existing live signal and net-exit thresholds, and now executes its own
budget independently rather than copying another account's fill. The old same-fill
comparison is hidden in this mode because it would be misleading.

Orders reserve the full all-in budget durably, then match against a fresh book
observed after submission and arrival. The arrival delay is the larger of one
second and the current read-only preflight duration: an explicitly conservative
estimate, not a measured order-POST latency. Only 50% of displayed depth is treated
as available. A FAK can partially fill; its remainder is canceled. Cash and shares
remain occupied until the simulated confirmation. Confirmation delay uses the 90th
percentile of up to 200 historical live submit-to-confirm samples (12 seconds if
fewer than ten samples). This delay is separate from matching delay. When book
connectivity is lost, saved matches keep their reserves and resume confirmation
when data returns. The polling worker never performs network I/O, and does not
wait for another strategy's REST preflight.

Both arrivals and matches are journaled in the respective Paper SQLite ledger.
After a restart, saved matches confirm exactly once; an arrival that was not
observed is canceled with a data-gap reason, not filled retrospectively. Cancellation
or ordinary order-expiry handling cannot prematurely release submitted reserves.
Official resolution waits for pending simulated fills. Winning payouts become
receivables first, with a modeled 60-second redemption delay before becoming
spendable cash. Fees are charged per filled level. Partial exits preserve remaining
shares and cost; a take-profit order cannot fill below its net-profit price floor.

The dashboard shows the active assumptions, saved budgets, confirmation state,
unfilled reasons and receivables. Open PnL uses net depth-based liquidation estimates;
missing depth is not a zero PnL or a fictional best-bid fill. A cached estimate is
labeled as such and is never executable. Historical runs are archived before a
reset. These are conservative observable-market simulations, not a guarantee of
real fills: hidden liquidity, queue priority, private API/chain failures, actual
redemption timing and multi-strategy wallet contention cannot be fully reproduced.

Protocol references: [Polymarket order types and price bounds](https://docs.polymarket.com/trading/place-orders),
[market-specific taker fees](https://docs.polymarket.com/trading/fees).

## 当前保留的五个策略

主账户、主账户（优化版）、主账户自适应成长版、主账户（优化极速赚钱版）、主账户（优化进阶版）。止盈止损和价值确认 V1 已退役，历史账本保留。

2026-11-11 按用户要求移除衡准与双翼的运行入口、页面和接口；已结束的实验保留精简历史归档。
原四策略的 Paper / 实盘账本、开关、金额和风险设置不变；自适应成长版新增实盘入口，默认关闭。
清理与归档清单见 `reports/data-cleanup-20261111-223500/`，历史数据位于 `data/archives/`。

## 自适应成长 V3（2026-11-16，当前版本）

仅升级 `MAIN_ADAPTIVE`；账户名、余额和历史不重置，其他三个策略的方向算法与设置保留，成长版实盘开关不自动打开。

- 用原始预测的 log-odds 作固定起点，以带固定 L2 正则的逻辑回归学习条件误差。连续特征包括原分数、方向、提交时买价、剩余时间，以及方向对齐的 10/30/60 秒动量、滚动波动、现价相对过去 60 秒均价偏离和盘口价差。单次分数修正限制为上下各 0.06，参数没有按历史利润寻优。
- 仍采用 72 小时样本半衰期、30 天窗口，每个市场最多一票，仅使用决策之前已经取得官方结算的标签。当前市场与未来结果不参与训练；基本有效样本或单项行情特征有效样本少于 40 时，分别保留基线或禁用该特征系数。
- 本机可从原同源行情缓存恢复历史特征，必须同时满足 `source_ms <= decision_at` 和 `received_ms <= decision_at`。优化版历史使用已保存的提交时间，不能用稍后的成交确认时间；V2 使用原始决策时间。缺失或过期特征保持缺失，不补造平盘和盘口。
- 旧 V2 表原样保留，V3 使用独立的 `context_training_samples_v3` 和 `context_observations_v3` 表。历史回补只用于训练与探索诊断。**旧记录重算的 V3 分数不能当作前向验证结果。** 每个新市场首次候选在结束前持久化模型版本、训练截止、训练指纹、行情特征、原分数、候选分数和官方含费成本。
- 正常 Paper 继续运行。只有 V3 新观察达到至少 40 个独立已结市场、跨度至少 24 小时，成对预测误差改善达到政策要求且近似 90% 下界为正，扣费报价理论收益为正并严格优于基线、理论回撤不劣于基线，才采用候选修正；滚动效果退化则继续基线。这个对照不模拟成交深度、资金占用、延迟与实际滑点，不能当作实盘收益证明。
- 页面区分“候选学习/修正已用于开仓”，显示 V3 已结成交实际采用修正的笔数、最近完整行情评估、覆盖数量，以及误杀赢家、避免输家、新增赢家/输家。`READY` 表示模型可计算，不表示修正已生效或已经更赚钱。

固定首版历史诊断使用 263 个已有机会：基线、V2、V3 的 Brier 分别为 0.21746、0.21759、0.21947（越低越好）；按每次机会单位预算计算的报价理论收益合计分别为 +6.4541、−0.2582、−2.1276。**V3 首版尚未优于基线，未据此启用修正。** 这些已经看过的历史仅作诊断，不能称作未接触测试集。后续真实前向结果决定是否值得采用，不能保证数据越多收益越高。

复现本机固定备份诊断（不写生产账本）：

```bash
.venv/macos/bin/python scripts/evaluate_adaptive_v3.py \
  --backup data/backups/adaptive-v3-20261116-183049 --evaluate
```

报告：`reports/adaptive-v3-20261116/feature-audit.json`、`chronological-diagnostic.json`。新环境没有本机历史时从其可用数据开始，原始基线仍可继续开仓。

## 自适应成长 V2（2026-11-15，历史版本）

以下保留 V2 的实现与诊断说明；当前运行版本为上文 V3。V2 仅作用于 `MAIN_ADAPTIVE`，原主账户和优化版方向模型不变。

- 原始分数与校准分数分开保存；旧成长版缺少原始分数的记录保留供复盘，不再作为原始概率样本重复学习。
- 决策只使用当时已经结算的历史，排除当前市场。同一五分钟市场最多贡献一票，防止不同策略的相同结算重复增加置信度。
- 历史权重采用 72 小时半衰期，最多使用 30 天；按方向、原始分数、价格和剩余时间逐级回退。少于最低有效样本时按原始分数继续评估，不为凑样本暂停交易。
- 用近期预测残差、20 票基准先验和不确定性折扣调整分数，上调最多 0.04、下调最多 0.08；比较校准分数与限价、手续费后的成本。参数为固定研究政策，未按旧利润搜索最优组合，分数也不是保证胜率。
- 新校准先作候选观察，当前采用原始基线分数继续运行。只有最近 7 天的新候选取得至少 40 个独立官方结算、观察跨度至少 24 小时，且成对预测误差改善的近似 90% 下界大于零、候选报价含费单位预算结果为正且不低于基线，才参与决策；滚动验证退化时自动退回基线。仅采用官方费率的新观察可以支持该激活门，旧式配置费率观察不能激活它。候选方向的报价理论结果不等于实际成交收益，这些阈值也不保证未来盈利。
- 成长版同一期不反向重复建仓。金额和持仓上限继续按现有界面设置执行，不加杠杆、不追亏加注。
- 新候选观察只在取得官方结算结果后进入学习；未成交的观察不是虚拟成交或利润。有效候选有不可变的模型版本、原始/校准分、成本、样本截止与报价时间快照。紧凑决策日志按模式限量轮换。
- 页面显示独立市场数、有效权重、旧分数隔离数量和本次费用后优势，避免把累计样本数当作效果证明。

真实成交模拟路径与实盘读取官方费率；兼容的旧式即时 Paper 仅使用配置中的模拟费率，不能将其结果当作实盘成交验证。现存策略历史不重置，V2 新成交可按模型版本和决策时间区分。

可复现的只读时序诊断：

```bash
.venv/macos/bin/python scripts/evaluate_adaptive_v2.py \
  --optimized-db data/strategy-experiments/main_optimized.sqlite3 \
  --adaptive-db data/strategy-experiments/main_adaptive.sqlite3 \
  --evaluation-start '2026-11-13T00:03:19+08:00' \
  --cutoff '2026-11-15T01:36:22+08:00' \
  --output reports/adaptive-v2-20261115/chronological-diagnostic.json
```

首轮固定政策的历史候选诊断未优于原始基线，因此未直接启用候选校准；运行时前向验证门与历史候选筛选诊断分开。

诊断严格使用先前结算数据，但历史成交价格只是事后执行成本代理；无法还原所有未提交信号、盘口深度、重试与资金约束，因此是已有成交的探索性筛选评估，不是完整策略回测，也没有未经查看的独立验证集。未来真实 Paper 记录才用于继续判断收益和回撤是否改善。验证方法参考 [时间顺序交叉验证](https://scikit-learn.org/stable/modules/cross_validation.html#time-series-split) 与 [概率校准](https://scikit-learn.org/stable/modules/calibration.html)。

## 主账户（优化极速赚钱版）（2026-11-16）

独立实验标识 `MAIN_OPTIMIZED_FAST`，政策版本 `main-optimized-fast-20261116-v1`。名称按用户要求保留，不代表保证更快盈利。优化目标是相同预算下减少低效入场，实际效果需要上线后的 Paper 对照；没有增加下注金额、放宽仓位或改动其他策略。

复盘扫描本机 33 份现存/备份账本，去重后为 565 条 Paper、466 条实盘记录；旧撮合与当前真实模拟分开。固定截止 `2026-11-16 01:37:02 +08:00`。当前真实模拟有 337 笔已结算，实盘有 465 笔已结算、1 笔待结。报告位于 `reports/optimized-fast-20261116/review.json` 和 `review.txt`，可用 `scripts/review_optimized_fast.py --help` 查看复现入口。

证据支持先测试每期一次成交和弱分数过滤：实盘 26 笔同一期反手按每笔 $5 归一合计亏损 $23.69；Paper 原始分数低于 0.66 的 24 笔等额亏损 $34.46。相同市场和方向的 209 组 Paper/实盘，等额收益分别为 $9.16/$8.77，主要累计差异来自不同机会选择。提高模型优势阈值或固定只做后半段没有一致支持，因此没有采用这两种做法。

| 项目 | 新策略规则 |
| --- | --- |
| 方向与结算模型 | 沿用优化版同源现价、60 秒 TWAP 和滚动波动预测；不使用新模型训练或未来标签 |
| 原始分数 | 至少 0.66；分数不是校准胜率 |
| 连续确认 | 在已有行情历史中检查 3 个源时间预测，跨度至少 6 秒，同向且均达到分数门槛；重复推送不能充数 |
| 入场时间与价格 | 剩余 20–285 秒，买价不超过 0.72，价差不超过 0.03，额外限价空间最多 0.01 |
| 成本 | 官方费用后分数减每份成本至少 0.02，分数/每份成本−1 至少 3%；提交前再次复核 |
| 深度和重复入场 | 完整新鲜盘口深度覆盖预算；每期最多一次成功入场，不反手；拒单仍可重试，待确认继续对账 |
| 初始账户 | 独立 Paper $100，不复制历史收益；真实模拟默认每笔 $5、最多 2 笔、亏损额度 $21，按界面设置执行 |
| 实盘 | 默认关闭，启用必须由用户明确操作；原有实盘设置不变 |

用户已有更严格的时间、分数、价格、价差和优势配置会继续生效。兼容的简化 Paper 路径继承全局金额/风险设置，并明确标记配置费率；默认 Mac 启动走官方费率与真实成交模拟。日志限量轮换，每笔新成交保留政策版本、原始分数、确认跨度、成本和提交前判断快照，便于后续同口径复盘。

历史仅可观察的“原记录每期首笔＋分数≥0.66”子集，按每笔 $5 归一后，累计收益从 $93.34 变成 $119.59，最大已实现回撤从 $44.85 变成 $33.72。但较早阶段收益下降，11 月 15 日筛选后仍亏损 $4.58。历史记录不能完整重放新增连续确认、拒单后的后续候选、盘口变化或资金限制，实盘也缺少原始分数；这些数字不是完整新策略回测或收益提升承诺。

## 主账户（优化进阶版）（2026-11-16）

独立实验 `MAIN_OPTIMIZED_PLUS`，政策版本 `main-optimized-plus-20261116-v1`。复用原优化版的 TWAP 预测、原始分数、时间、价格、价差、深度和费用规则；不叠加 FAST 的 0.66 分数或六秒确认，不修改既有四个账户的算法、账本和保存设置。

| 项目 | 规则 |
| --- | --- |
| 同期入场 | 每个五分钟市场最多一次成功入场，不反手、不重复；待确认继续核实，失败/拒单仍可重试。跨市场持仓上限仍按界面设置 |
| Up 短线动量 | 过去 30 秒 BTC 对数涨幅超过 2 bps（约 0.02%）时暂不买 Up，动量回落后重新评估；Down 不增加此门槛 |
| 行情因果性 | 使用同源 `price.crypto`，源时间不超过本次预测且接收时间不超过本次决策；Up 缺失、过期或断档时显示等待原因，不永久禁入该期 |
| 晚入场 | 开始 120 秒后只记录诊断，不作为禁入条件 |
| 成本 | 沿用原始费用后优势门槛（至少 0.02）；提交前按官方费率重新验证，并把最终决策绑定订单 |
| 初始账户 | 独立 Paper $100；真实模拟默认每笔 $5、最多 2 笔、亏损额度 $30，Paper / 实盘均按界面保存值执行 |
| 实盘入口 | 独立卡片和勾选项，默认关闭；其他策略已有开关保持原值 |

2 bps 阈值来自对原优化版实盘历史的探索性分析，不是未经查看的测试集结果。固定复盘截至 `2026-11-16 21:31:28 +08:00`，报告位于 `reports/optimized-live-review-20261116-213128/`。不能把历史筛掉的亏损直接算作新策略可获得的利润；后续信号、资金占用和成交机会会变化，实际收益与胜率需要前向验证。

新策略详情提供**从新账户创建起的同期 Paper 对照**：原优化版与进阶版各自实际已结成交的净收益、胜率、最大已实现回撤、未结笔数及每笔 $5 成本归一收益。按开仓时间纳入，排除新策略启动前已开的旧仓；未结仓不计入已实现盈亏。它比较真实发生的两组模拟成交，不是同一机会的反事实回放，也不含未实现回撤。无已结样本时胜率显示为空。

每笔新订单保留政策版本、决策时间、原始分数、30 秒动量、官方含费成本和最终提交快照；诊断日志限量轮换。共享钱包资金仍会影响启用多个实盘策略后的成交机会。默认 Mac 启动使用真实成交模拟，兼容简化 Paper 则继承全局金额/风险设置并标记配置费率。

## Safety

This project is not an investment recommendation. Live trading can place real Polymarket CLOB orders when enabled, SDK credentials are present, and the dashboard risk confirmation is checked. Use a separate low-balance wallet: the configured initial amount is enforced by this bot's software accounting only and cannot prevent manual wallet spending or external orders.
