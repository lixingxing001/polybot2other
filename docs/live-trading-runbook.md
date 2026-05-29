# SINGLE_FAK_REAL Live Trading Runbook

## Scope

This runbook is for `SINGLE_FAK_REAL`, the first live-trading variant. Paper trading and strategy experiments continue running for comparison. Live trading uses `data/live/single_fak_real.sqlite3` and `data/live/live-settings.json`.

## Required Preconditions

1. Use a separate low-balance wallet. The dashboard `initial_balance` is only a software budget guard.
2. Confirm that using Polymarket from the operating location is allowed. Do not use this project to bypass access restrictions.
3. Fund the trading wallet with enough Polymarket CLOB collateral/pUSD for the configured stake and Polymarket minimum order size.
4. Approve the Polymarket CLOB collateral/pUSD allowance for buy orders.
5. For manual sell, the wallet must hold the conditional token and allowance for the shares being sold.

The dashboard and preflight call Polymarket's geoblock endpoint from the running service environment. If it reports `blocked=true`, live arming and new BUY orders are blocked. This is a compliance guard, not a workaround; users are still responsible for their account, residency, physical operating location, and local laws.

For live BUY orders, the bot passes the configured per-order stake to the SDK field named `user_usdc_balance`. In CLOB V2 this is still a dollar-denominated budget guard, but the wallet asset to fund/approve is Polymarket CLOB collateral/pUSD. If Polymarket fee logic would otherwise push total cost above that stake, the SDK can reduce the signed buy amount instead of exceeding the bot's software budget.

## Environment Setup

```bash
rtk proxy cp .env.live.example .env.live
rtk proxy chmod 600 .env.live
```

Fill these values in `.env.live`:

```text
POLYBOT2OTHER_LIVE_TRADING_RUNTIME_ENABLED=true
POLYBOT2OTHER_LIVE_PRIVATE_KEY=
POLYBOT2OTHER_LIVE_SIGNATURE_TYPE=
POLYBOT2OTHER_LIVE_FUNDER_ADDRESS=
POLYBOT2OTHER_LIVE_API_KEY=
POLYBOT2OTHER_LIVE_API_SECRET=
POLYBOT2OTHER_LIVE_API_PASSPHRASE=
```

`POLYBOT2OTHER_LIVE_API_KEY`, `POLYBOT2OTHER_LIVE_API_SECRET`, and `POLYBOT2OTHER_LIVE_API_PASSPHRASE` are optional only when all three are blank; in that mode the SDK derives API credentials from the private key.

Blank values in env files are recorded in readiness as `empty_keys` and are not loaded into the process environment. This keeps an unfilled `.env.live` template from masking real values supplied by `.env.local`, `.env`, or the process environment.

If `.env.live` contains `POLYBOT2OTHER_LIVE_PRIVATE_KEY` or API credential fields, readiness blocks live trading unless the file permissions are owner-only, for example `0o600`. This prevents a real private key from being left group/world-readable on the host.

`POLYBOT2OTHER_LIVE_TRADING_RUNTIME_ENABLED=false` is the process-level hard kill switch. Use it for Paper-only operation or emergency maintenance when you do not want this process to instantiate the live runner at all. It is stronger than the dashboard live switch and must be set back to `true` before real orders can be submitted.

If any live credential value changes while the process is running, the bot discards the cached SDK client plus wallet/token/open-orders caches and rebuilds them on the next official call. A process restart is still recommended after changing production credentials, but stale in-memory wallet state is not reused.

Signature type mapping:

```text
0 = EOA
1 = proxy wallet
2 = Gnosis Safe
3 = deposit wallet
```

New API users should normally use `3 = deposit wallet` and set `POLYBOT2OTHER_LIVE_FUNDER_ADDRESS` to the deposit wallet address. The deposit wallet, not just the owner EOA, must hold the collateral/pUSD and have the required CLOB allowance. If `0 = EOA` is used, the funder address must be the same address derived from `POLYBOT2OTHER_LIVE_PRIVATE_KEY`; readiness blocks live trading when those two addresses differ.

To fill or update `.env.live` without sending secrets through chat, use the local setup tool:

```bash
rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_env_setup --service-url http://127.0.0.1:8791
```

The tool prompts in the terminal with hidden input for secret values, preserves non-credential `.env.live` settings, writes `chmod 600 .env.live`, and optionally calls the running dashboard's credential reload endpoint. It prints only masked addresses, booleans, and errors.

## Start

```bash
rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.web --host 127.0.0.1 --port 8791
```

The service automatically loads `POLYBOT2OTHER_*` values from `.env.live`, `.env.local`, and `.env`. Existing process environment variables take precedence. Use `POLYBOT2OTHER_ENV_FILE=/absolute/path/to/file` to point the service at a different local env file.

If you edit live credential values while the dashboard is already running, click `重载凭证` or run:

```bash
rtk proxy sh -c 'curl -s -X POST http://127.0.0.1:8791/api/live-reload-credentials | .venv/bin/python -m json.tool'
```

This reloads only private key, signature type, funder address, and CLOB API credential env keys, then clears cached SDK authentication state. It does not submit, cancel, or sell orders. Changes to database paths, default risk config, or other non-credential env settings still require a service restart or the dedicated settings API.

Startup always re-arms live trading to off. If `data/live/live-settings.json` was saved with `enabled=true`, the new process rewrites it to `enabled=false` and shows `服务启动后实盘开关已自动关闭，需要人工重新预检并开启`. This prevents an automatic restart from resuming real orders before the operator checks the current wallet, market, and API state.

Run only one live-enabled service for the same `POLYBOT2OTHER_LIVE_TRADING_SETTINGS_PATH`. When live is enabled, the process holds a sibling lock file such as `data/live/live-settings.json.lock`; another process using that same settings path will fail to enable live trading and report a process-lock blocking reason.

Open:

```text
http://127.0.0.1:8791
```

## Preflight Before Enabling

```bash
rtk proxy sh -c 'curl -s "http://127.0.0.1:8791/api/live-preflight?include_snapshot=false" | .venv/bin/python -m json.tool'
```

You can run the same read-only check without the web server:

```bash
rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --pretty --require-arming-ready
rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --service-url http://127.0.0.1:8791 --pretty --require-arming-ready
rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_doctor --pretty --require-one-shot-ready
rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_doctor --service-url http://127.0.0.1:8791 --pretty --require-one-shot-ready
```

Before enabling live trading, use `arming_ready=true`. This means all checks except the live switch itself passed. After enabling live trading, the live path is ready only when `ready=true` and `can_place_next_order=true`. These results are moment-in-time snapshots; the bot still rechecks the latest market, quote, wallet balance, wallet allowance, geoblock status, official open orders, and risk limits immediately before every live order.

`blocked_checks` lists the exact remaining blockers. The CLI exits with code `2` when `--require-arming-ready` or `--require-ready` is requested and the corresponding condition is not met.

The CLI prints only `live_preflight` by default so the terminal output stays readable. Add `--include-snapshot` only when debugging broader dashboard state. Use `live_preflight --service-url http://127.0.0.1:8791` for the final arming check when the dashboard service is running, because that reads `/api/live-preflight` from the same process that will evaluate the live order path.

`live_doctor` is a read-only wrapper around settings and preflight. It returns `status`, `ready_for_one_shot_now`, `can_wait_for_one_shot`, SDK compatibility, fatal blockers, waitable blockers, credential setup status, next actions, the recommended first-order command, and the post-order evidence checklist. Use it as the final terminal check before running `live_once`. The HTTP endpoint defaults to `include_snapshot=false` to keep the response small.

Use `live_doctor --service-url http://127.0.0.1:8791` for the final check when the dashboard service is already running. That mode reads `/api/live-doctor` from the live process, so the result uses the same in-memory current market, settings, and latest browser/REST quote snapshot that the dashboard would use.

The doctor-recommended one-shot command is generated from the current live preflight stake. If a same-market live position already exists, this means the command uses the locked current-market stake instead of the newly edited dashboard stake; otherwise it uses the configured live `stake_dollars`. This keeps the confirmation cap aligned with the actual amount the next live order would try to spend.

If doctor reports `fatal_one_shot_blockers=["min_order_size", ...]`, the current market's official minimum order is above the configured stake. Check `first_order.stake_requirement`: when `can_fix_by_settings_update=true`, set dashboard `单笔金额` to `suggested_stake_dollars` or higher, save, and rerun doctor. Do not run one-shot with the old smaller `--max-stake`; the bot will correctly refuse to submit.

When all earlier checks pass, preflight also asks the official SDK to construct and sign the exact FAK order parameters for the current signal. This signing check does not call `post_order`, does not submit to CLOB, and does not return the signed order payload.

`/api/live-settings` also exposes `readiness.env_files`, `readiness.credential_presence`, and `readiness.credential_addresses`. These fields show whether the service actually loaded `.env.live`, whether the required credential fields are present, whether the secret env file permissions are owner-only, and which signer/funder public addresses will be used, but never expose private key, API secret, or passphrase values.

The same readiness path reports the installed `py_clob_client_v2` version and checks that the package still exports the classes, enums, and methods used for signing, posting, balance/allowance checks, order reads, trade reads, open orders, and cancel-all. If this SDK compatibility check fails, leave live disabled and reinstall the pinned project dependencies before trading.

The dashboard also shows `official open N` from the official CLOB `get_open_orders` response. This value must be zero before live can be armed and before the runner submits a new BUY. Use `刷新挂单` or this command for a forced refresh:

```bash
rtk proxy sh -c 'curl -s http://127.0.0.1:8791/api/live-open-orders | .venv/bin/python -m json.tool'
```

When credentials are missing, this check returns `skipped=true` and does not repeatedly hit official APIs. Normal dashboard polling uses a short cache for performance, but live arming and BUY submission force a fresh official open-orders read.

## Enable Live Trading

1. In the dashboard, set `initial_balance`, `stake_dollars`, max position count, daily loss stop, total drawdown stop, retry count, and retry delay.
2. Check the risk confirmation box.
3. Click `保存`.
4. Click `预检`, or run the CLI preflight command above.
5. Confirm the preflight `geo_access` and `official_open_orders_clear` checks are passing.
6. Turn on the top `实盘` switch only after `arming_ready=true`.

If `stake_dollars` is changed while the current market already has an open live position, the current market keeps using the original stake for any same-market reversal leg. The new stake is applied from the next market. The preflight output shows this through `software_account.stake_source=current_market_open_trade` and the dashboard labels the budget as `当前市场锁定`.

After live is enabled, the next eligible `SINGLE_FAK` signal may submit a real FAK order through the official CLOB SDK only if a fresh official open-orders check is still clear.

If credentials, geoblock status, collateral/pUSD balance, collateral/pUSD allowance, software cash, official open orders, or risk confirmation are not ready, the server keeps `enabled=false` and shows the blocking reason in the live status. This means the switch is an armed-state control, not a cosmetic UI flag.

`retry_count` and `retry_delay_ms` apply to create/sign, post, collateral balance/allowance sync, conditional token balance/allowance sync, and official order/trade rechecks. Retries are for transient timeout/network/429/5xx failures; non-retryable API errors still fail fast.

## First Live Order One-Shot

For the first real-money validation, prefer the one-shot path over leaving the dashboard loop enabled indefinitely. Start with the normal live switch off. It still uses the same `SINGLE_FAK_REAL` logic and official SDK path, but it requires an explicit confirmation string and a max stake cap, then disables live again by default after one live run.

From the dashboard, click `首单检查` first. The `执行首单` button remains disabled while fatal blockers exist. When the button unlocks, it refreshes doctor again, asks for the exact phrase `PLACE_REAL_ORDER`, posts `/api/live-once` with the doctor-recommended max stake, and renders the returned blocked/order/evidence fields. If you prefer terminal output and a machine-readable response, use the CLI below instead.

CLI:

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

Dashboard service API:

```bash
rtk proxy sh -c 'curl -s -X POST http://127.0.0.1:8791/api/live-once \
  -H "content-type: application/json" \
  -d "{\"confirm\":\"PLACE_REAL_ORDER\",\"acknowledge_compliance\":true,\"max_stake_dollars\":2,\"disable_after\":true,\"wait_ready_seconds\":180,\"ready_poll_seconds\":2,\"reconcile_wait_seconds\":20,\"include_evidence\":true}" \
  | .venv/bin/python -m json.tool'
```

Use `--service-url` for the first order when the dashboard service is running. It posts to `/api/live-once`, so the one-shot attempt uses the same in-process current market, settings, and browser/REST quote snapshot that the dashboard sees. Without `--service-url`, the CLI creates a fresh local bot and depends on REST fallback data.

Use `disable_after=true` for the first order. `--wait-ready-seconds` / `wait_ready_seconds` lets one-shot wait through transient blockers such as a new market whose official target price has not propagated yet, a temporary `NO_TRADE` signal, or thin orderbook depth. It does not wait through credentials, geoblock, wallet balance/allowance, software cash, risk confirmation, or official open-orders blockers; those fail fast and must be fixed manually. `--wait-reconcile-seconds` / `reconcile_wait_seconds` asks the command to poll official order/trade state briefly after the order attempt while it still holds the live process lock, so the output may already show `FILLED(完全成交)`, `CANCELED(已取消)`, or still `PENDING(待官方确认)`. Successful one-shot output includes `live_once.evidence` by default; use it as the first post-order evidence package before any re-enable decision. If an order attempt is submitted or has an order id, the service writes a sanitized audit JSON under `data/live/audit/` and returns the path in `live_once.audit`; this gives you a local evidence file even if terminal output is lost. Use CLI `--no-evidence` or API `include_evidence=false` only when you intentionally want smaller output. Use `--leave-enabled` or `disable_after=false` only after the first order id, local order row, official open-orders state, and wallet balance movement have all been checked.

If one-shot is blocked before submission, the CLI/API response includes `live_once.blocked=true`, `blocked_keys`, `fatal_blocked_keys`, `waitable_blocked_keys`, and the latest `live_once.preflight`. Use `fatal_blocked_keys` for items that must be fixed manually, such as credentials, geoblock, wallet balance/allowance, software cash, risk confirmation, or official open orders. Use `waitable_blocked_keys` for market/target/signal/depth conditions that can be retried with a wait window.

## Evidence To Check After A Live Order

The one-shot response already includes `live_once.evidence` by default. If you need to re-check evidence later, collect the read-only evidence package again:

Submitted one-shot attempts also save a sanitized local artifact:

```text
data/live/audit/live-once-*.json
```

The audit artifact omits raw responses and secret-like fields. If the file cannot be written, `live_once.audit.saved=false` is returned, but the order result remains visible in the API/CLI response.

```bash
rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_evidence \
  --service-url http://127.0.0.1:8791 \
  --external-order-id OFFICIAL_ORDER_ID \
  --pretty
```

Equivalent raw dashboard service API:

```bash
rtk proxy sh -c 'curl -s "http://127.0.0.1:8791/api/live-evidence?external_order_id=OFFICIAL_ORDER_ID&force=true&include_snapshot=false" | .venv/bin/python -m json.tool'
```

This evidence package does not submit, sell, or cancel orders. Prefer the `--service-url` command after the first order so evidence is collected from the same dashboard process that submitted the order. The HTTP endpoint defaults to `include_snapshot=false` to keep the response small. It should include local software-account metrics, readiness/wallet state, official open orders, open live trades, pending live orders, recent live orders/trades, and the sanitized local order row for the official order id.

1. `订单流水` with `account_scope=live` should show `execution_mode=LIVE`.
2. `external_order_id` should match the official CLOB order id when Polymarket returns one.
3. `GET /api/live-open-orders` should show whether the official CLOB account still has resting orders.
4. `FILLED(完全成交)` means local accounting found official fill evidence or matched response amounts.
5. `PENDING(待官方确认)` means the CLOB returned an order id without confirmed fill; the bot will poll official order/trade endpoints.
6. `CANCELED(已取消)` means official order/trade state showed a no-fill cancellation, expiration, or unmatched FAK result.
7. `REJECTED(已拒绝)` means official order/trade state showed `ORDER_STATUS_INVALID`, rejected, failed, or error. No local live position should be opened for that order.

While a live buy order is `PENDING(待官方确认)`, do not expect the bot to submit another live buy for the same market, even if the signal reverses. `SINGLE_FAK_REAL` waits for the pending buy to become filled, canceled, or timed out before allowing another entry for that market.

After a live BUY receives an official order id, the bot writes a local `PENDING(待官方确认)` order before extra order/trade reconciliation. If official status says matched but no official matched amounts are available and the immediate recheck fails, the order stays pending instead of opening a local position from the pre-order orderbook estimate. If local accounting fails after the official order may have reached CLOB, live trading is disabled immediately; check the pending order, official CLOB order id, and `/api/live-open-orders` before re-enabling.

Manual sell uses the same pending-first rule. If `/api/live-sell` receives an order id, the exit order is written locally as `PENDING(待官方确认)` before shares are closed. If the response has no confirmed fill, the local live position remains open. The bot closes shares only after official order/trade recheck proves the sell fill amount; official no-fill terminal status changes the exit order to canceled.

If a manual SELL may have reached CLOB but local accounting fails, live trading is disabled immediately and the pending exit order blocks duplicate manual sell for the same trade. Check the official order id, conditional token balance, open orders, and local pending exit order before re-enabling.

Do not submit another manual sell for a trade that already has a `PENDING(待官方确认)` exit order. The server blocks duplicates, and the dashboard changes the button to `卖出确认中` until official reconciliation finishes.

If dashboard/manual sync overlaps with the background tick, the live runner skips the overlapping live run instead of waiting and potentially submitting the same signal twice. Check `runtime.live_trading.overlap_skip_count` in `/api/status` if you suspect skipped overlapping live runs.

The live runner also rechecks the live switch, geoblock status, and official open orders immediately before official BUY submission. If `实盘` is turned off, geoblock status is not clear, or official open orders are no longer clear while the runner is still doing wallet/depth checks, that in-flight run stops before calling `post_order`.

The same process lock is also used by manual `/api/live-sell` when the live switch is off. This allows emergency reduction of an open live position while still preventing two service processes from submitting duplicate SELL orders against the same live database and wallet.

## Stop Or Roll Back

1. Click `实盘急停` to save `enabled=false` and request official CLOB `cancel_all`.
2. If the dashboard is unavailable, run:

```bash
rtk proxy sh -c 'curl -s -X POST http://127.0.0.1:8791/api/live-emergency-stop | .venv/bin/python -m json.tool'
```

3. If needed, stop the server process.
4. To reset only live local accounting, stop the server and remove `data/live/single_fak_real.sqlite3` plus its SQLite WAL/SHM files.
5. Do not delete Paper or strategy experiment databases unless the comparison history is no longer needed.

`实盘急停` does not blindly mark local `PENDING(待官方确认)` orders as canceled. Those rows continue through official order/trade reconciliation or local FAK pending timeout, so the software budget is not released before there is no-fill evidence.
