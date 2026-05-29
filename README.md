# polybot2other

Real Polymarket BTC 5-minute Up/Down paper and live-trading dashboard.

The default mode still runs Paper trading continuously. Live trading is opt-in and disabled until the dashboard setting is explicitly enabled:

- Initial balance defaults to `$100.00`.
- Only BTC 5-minute markets are tracked.
- Current market discovery uses Polymarket Gamma event slugs such as `btc-updown-5m-<window_start_unix>`.
- Orderbook quotes use Polymarket CLOB token ids for the active Up/Down market.
- The browser dashboard uses Polymarket CLOB market WebSocket and RTDS crypto price WebSocket where available.
- If the browser WebSocket feed is stale or closed, the backend falls back to CLOB REST orderbook and public BTC price APIs for display and paper-trading continuity.
- Paper entries are simulated as CLOB-style orders. The default entry mode is FAK, walks visible ask levels up to the execution limit, and charges the configured taker fee per filled level.
- GTC/GTD/POST_ONLY are simulated as resting maker orders. GTC can rest until market end, GTD expires after `POLYBOT2OTHER_PAPER_GTD_SECONDS`, and POST_ONLY additionally requires queue time, price-through-limit movement, and partial queue fills.
- Resting maker partial fills from the same paper order are aggregated into one open position. Tiny resting fills below `$0.01` are ignored, and residual reserved cash at or below `$0.05` is released as dust instead of creating `$0.00` open positions.
- Paper order attempts and per-level paper fills are stored separately from positions for execution-quality review.
- Strategy experiments can run 11 isolated Paper combinations: the original `SINGLE/PAIR + FAK/GTC/GTD/POST_ONLY` set plus `SINGLE_FAK_STRICT`, `SINGLE_FAK_REVERSAL`, and `SINGLE_FAK_STOP_AND_FLIP`.
- `SINGLE_FAK_REAL` uses a separate live SQLite database and follows the current `SINGLE_FAK` paper strategy behavior: FAK taker entry and legacy same-market opposite-side reversal allowed after any pending live buy for that market has settled.
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
POST /api/live-once
GET /api/strategy-experiments
GET /api/strategy-experiments?variant_id=PAIR_GTD&trade_limit=50&order_limit=50
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

For the end-to-end live setup checklist, use `docs/live-trading-runbook.md`. Copy `.env.live.example` to `.env.live`, run `chmod 600 .env.live`, then fill the private values locally. `.env.live` is ignored and must hold the real private values only on the local machine.

For safety, a saved `enabled=true` live switch is not preserved across process restarts. On startup the service rewrites live settings to `enabled=false` and shows that manual preflight/re-enable is required before any new real order can be submitted.

## Safety

This project is not an investment recommendation. Live trading can place real Polymarket CLOB orders when enabled, SDK credentials are present, and the dashboard risk confirmation is checked. Use a separate low-balance wallet: the configured initial amount is enforced by this bot's software accounting only and cannot prevent manual wallet spending or external orders.
