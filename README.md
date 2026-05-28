# polybot2other

Real Polymarket BTC 5-minute Up/Down paper trading dashboard.

This project is intentionally paper-only:

- Initial balance defaults to `$100.00`.
- No private key, API secret, or live order endpoint is used.
- Only BTC 5-minute markets are tracked.
- Current market discovery uses Polymarket Gamma event slugs such as `btc-updown-5m-<window_start_unix>`.
- Orderbook quotes use Polymarket CLOB token ids for the active Up/Down market.
- The browser dashboard uses Polymarket CLOB market WebSocket and RTDS crypto price WebSocket where available.
- If the browser WebSocket feed is stale or closed, the backend falls back to CLOB REST orderbook and public BTC price APIs for display and paper-trading continuity.
- Paper entries are simulated as CLOB-style orders. The default entry mode is FAK, walks visible ask levels up to the execution limit, and charges the configured taker fee per filled level.
- Paper order attempts and per-level paper fills are stored separately from positions for execution-quality review.
- Trades, settlements, and equity curve are stored in SQLite.
- Recent trades include a trade-level settlement source. `polymarket_official` means the market winner came from Polymarket Gamma resolved prices; `chainlink_fallback` means the bot used the local Chainlink price against the target because the official winner was not available yet; `early_exit` means the position was closed before market settlement. The bot periodically rechecks fallback settlements and upgrades or corrects them when the official outcome appears. When Polymarket exposes `finalPrice` / `priceToBeat` in event metadata, the bot records those official settlement prices for final price and final distance display; if metadata is missing, it falls back to a one-time Polymarket page payload parse after settlement.

## Run

```bash
rtk proxy python3 -m polybot2other.web --host 127.0.0.1 --port 8787
```

Open:

```text
http://127.0.0.1:8787
```

## Test

```bash
rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests
```

## Runtime Files

```text
data/polybot2other-real-btc.sqlite3
```

## API

```text
GET /api/recent-trades?limit=100&offset=0&start_at=1779870000&end_at=1779873600
GET /api/orders?limit=20&offset=0&status=all
GET /api/order-fills?order_id=1
POST /api/cancel-order {"order_id":1}
POST /api/cancel-orders {"scope":"current_market"}
POST /api/cancel-orders {"scope":"all"}
```

`/api/orders` status filters: `all`, `active`, `filled`, `canceled`, `expired`, `rejected`.

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
POLYBOT2OTHER_GAMMA_URL=https://gamma-api.polymarket.com
POLYBOT2OTHER_CLOB_URL=https://clob.polymarket.com
```

## Safety

This project is not an investment recommendation and does not place real orders. Live trading requires a separate design review for private key handling, API credentials, order signing, slippage, cancellation, loss limits, audit logs, and legal/compliance constraints.
