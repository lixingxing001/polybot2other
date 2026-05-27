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
- Trades, settlements, and equity curve are stored in SQLite.

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

## Environment

```text
POLYBOT2OTHER_INITIAL_BALANCE=100
POLYBOT2OTHER_DB_PATH=data/polybot2other-real-btc.sqlite3
POLYBOT2OTHER_TICK_SECONDS=2
POLYBOT2OTHER_STAKE_DOLLARS=5
POLYBOT2OTHER_MAX_OPEN_TRADES=2
POLYBOT2OTHER_MIN_CONFIDENCE=0.62
POLYBOT2OTHER_MAX_ENTRY_PRICE=0.72
POLYBOT2OTHER_GAMMA_URL=https://gamma-api.polymarket.com
POLYBOT2OTHER_CLOB_URL=https://clob.polymarket.com
```

## Safety

This project is not an investment recommendation and does not place real orders. Live trading requires a separate design review for private key handling, API credentials, order signing, slippage, cancellation, loss limits, audit logs, and legal/compliance constraints.
