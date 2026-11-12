# EdgePulse

The dashboard has four isolated Paper accounts: the original **主账户**, **主账户止盈止损** (`MAIN_TP_SL`), **主账户（优化版）** (`MAIN_OPTIMIZED`), and **价值确认 V1** (`VALUE_CONFIRM_V1`). Existing strategy rules and histories remain intact. In the default managed-live runtime, the Paper accounts share the verified PolyBolt spot/TWAP and CLOB collector used by live execution. The optimized account keeps its own model, ledger and journal while borrowing the same reference history; the exit account still mirrors original-main Paper entries and applies its existing exit policy.

The dashboard also provides opt-in real execution for these four strategies. The global live switch and the corresponding strategy switch must both be enabled before new real BUY orders are allowed; all switches default off in a new live ledger. Each strategy has its own fee-inclusive order budget and accounting, while the actual wallet balance is shared. Turning either switch off stops new entries and continues managing existing positions, including the live take-profit/stop-loss strategy. Paper accounts continue as independent controls.

The top metrics and tables follow the selected strategy's effective Paper/live mode by default. A separate data-view selector can inspect either ledger without enabling trading. Wallet cash comes from authenticated CLOB balance queries, and portfolio value comes from the official Data API; failed reads are shown as unavailable or stale rather than a fabricated zero. Live strategy P&L is based on confirmed official fills and the market's fee schedule. Resolved winning shares are shown as pending redemption and are never credited to the displayed cash balance before the wallet API reports the cash.

Live execution uses its own validated TWAP reference and fresh order books. It does not copy Paper fills. Signed order hashes are saved before a single POST; uncertain results retain reservations and are reconciled rather than blindly resubmitted. Partial fills retain remaining shares. Settings and manual exits are restricted to same-origin loopback requests with a per-process control token. The live ledger and public reference cache are under `data/live-accounts/`; credentials remain only in the existing owner-readable environment file. No blockchain approvals, funding transfers, or redemptions are performed automatically.

The exit experiment reads the original main ledger in SQLite read-only mode and mirrors only new Paper FAK entries, retaining their actual cost, buy fees and shares for a matched-entry comparison. It does not retroactively copy old positions or re-enter after an early sale. Its initial test thresholds are +20% net profit and −40% net loss, with a 500 ms simulated sell delay, half of displayed bid depth and a one-cent adverse price haircut. Thresholds are experimental; stop loss is a trigger, not a guaranteed maximum loss. Partial exits preserve unsold shares, and unfilled exits do not receive invented proceeds. The account stores its own sales, entry links and policy in `data/strategy-experiments/main_tp_sl.sqlite3`; observations continue after early sales until expiry in `main_tp_sl.observations.jsonl`. The dashboard compares only officially resolved matched entries and reports both saved losses and forgone winner profits. Late observations, insufficient funds and paused entries are recorded as skipped instead of backfilled. The original main account supplies entry decisions and its existing entry risk controls; this exit experiment only adds its own cash constraint. It never enables live orders.

Read the optimized-account specification at `http://127.0.0.1:8791/static/optimized-main.html`. Its initial virtual equity is aligned with the original main account at creation; subsequent restarts neither re-fund it nor reset comparison history. The dashboard's same-period comparison excludes original historical profit and carryover positions, distinguishes pending exposure, and reports actual P&L/percentage-point differences only after trades exist. Model changes and the early mid-price filter are forward-testing hypotheses, not proven profit improvements.

The packaged [historical review](src/polybot2other/static/strategy-review.html) remains available. Detailed loss, exit and trading-hour analyses are generated locally under `reports/`. Their Python analysis/rendering scripts are versioned; database snapshots, generated reports, audit manifests and local configuration are excluded from Git. These research scripts require the corresponding local snapshots and backup metadata. Retired strategy sources and ledgers are archived under `data/backups/strategy-retirement-20261026-225605/`. Value Confirm retains its own Paper history and original Paper sizing controls. Live operation remains opt-in through the new dashboard switches.

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

The Mac launcher enables `POLYBOT2OTHER_PAPER_REALISTIC_EXECUTION=true`. The four
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

## 当前保留的四个策略

主账户、主账户止盈止损、主账户（优化版）、价值确认 V1。

2026-11-11 按用户要求移除衡准与双翼的运行入口、页面和接口；已结束的实验保留精简历史归档。
原四策略的 Paper / 实盘账本、开关、金额和风险设置不变。
清理与归档清单见 `reports/data-cleanup-20261111-223500/`，历史数据位于 `data/archives/`。

## Safety

This project is not an investment recommendation. Live trading can place real Polymarket CLOB orders when enabled, SDK credentials are present, and the dashboard risk confirmation is checked. Use a separate low-balance wallet: the configured initial amount is enforced by this bot's software accounting only and cannot prevent manual wallet spending or external orders.
