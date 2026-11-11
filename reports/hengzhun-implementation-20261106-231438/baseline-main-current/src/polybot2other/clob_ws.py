from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import ssl
import struct
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import MarketRound


logger = logging.getLogger(__name__)
CLOB_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_WS_URL = "wss://ws-live-data.polymarket.com"
OKX_SPOT_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
BINANCE_SPOT_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@ticker"
CLOB_WS_PING_SECONDS = 10.0
RTDS_WS_PING_SECONDS = 5.0
SPOT_WS_PING_SECONDS = 10.0
CLOB_WS_READ_TIMEOUT_SECONDS = 1.0
CLOB_WS_RECONNECT_INITIAL_SECONDS = 0.5
CLOB_WS_RECONNECT_MAX_SECONDS = 5.0
MAX_CLOB_WS_LEVELS = 50
RTDS_UNPARSED_LOG_INTERVAL_SECONDS = 30.0
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
SPOT_WS_SOURCE_OKX = "okx"
SPOT_WS_SOURCE_BINANCE = "binance"
RTDS_CHAINLINK_BTC_SYMBOL_KEYS = {"btcusd"}
RTDS_CRYPTO_BTC_SYMBOL_KEYS = {"btcusdt"}


class WebSocketProtocolError(RuntimeError):
    pass


class _StdlibWebSocket:
    def __init__(self, sock: ssl.SSLSocket) -> None:
        self.sock = sock

    @classmethod
    def connect(cls, url: str, timeout_seconds: float) -> "_StdlibWebSocket":
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "wss":
            raise ValueError("only wss:// endpoints are supported")
        host = parsed.hostname
        if not host:
            raise ValueError("websocket URL missing host")
        port = parsed.port or 443
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        raw_sock = socket.create_connection((host, port), timeout=timeout_seconds)
        try:
            context = ssl.create_default_context()
            sock = context.wrap_socket(raw_sock, server_hostname=host)
            sock.settimeout(timeout_seconds)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "User-Agent: polybot2other/0.1\r\n"
                "\r\n"
            )
            sock.sendall(request.encode("ascii"))
            header = cls._read_http_header(sock)
            cls._validate_handshake(header, key)
            sock.settimeout(CLOB_WS_READ_TIMEOUT_SECONDS)
            return cls(sock)
        except Exception:
            raw_sock.close()
            raise

    @staticmethod
    def _read_http_header(sock: ssl.SSLSocket) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketProtocolError("websocket handshake closed before headers")
            data.extend(chunk)
            if len(data) > 64 * 1024:
                raise WebSocketProtocolError("websocket handshake header too large")
        return bytes(data)

    @staticmethod
    def _validate_handshake(header: bytes, key: str) -> None:
        text = header.decode("iso-8859-1", errors="replace")
        lines = text.split("\r\n")
        status = lines[0] if lines else ""
        if " 101 " not in status:
            raise WebSocketProtocolError(f"websocket handshake failed: {status}")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()).decode("ascii")
        actual = headers.get("sec-websocket-accept")
        if actual and actual != expected:
            raise WebSocketProtocolError("websocket handshake accept key mismatch")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def _send_pong(self, payload: bytes) -> None:
        self._send_frame(0xA, payload)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        first = 0x80 | (opcode & 0x0F)
        length = len(payload)
        mask = os.urandom(4)
        if length < 126:
            header = struct.pack("!BB", first, 0x80 | length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", first, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", first, 0x80 | 127, length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def read_text(self, timeout_seconds: float = CLOB_WS_READ_TIMEOUT_SECONDS) -> str | None:
        self.sock.settimeout(timeout_seconds)
        fragments: list[bytes] = []
        while True:
            frame = self._read_frame()
            if frame is None:
                return None
            fin, opcode, payload = frame
            if opcode == 0x8:
                raise WebSocketProtocolError("websocket closed by server")
            if opcode == 0x9:
                self._send_pong(payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                fragments = [payload]
                if fin:
                    return payload.decode("utf-8", errors="replace")
                continue
            if opcode == 0x0 and fragments:
                fragments.append(payload)
                if fin:
                    return b"".join(fragments).decode("utf-8", errors="replace")

    def _read_frame(self) -> tuple[bool, int, bytes] | None:
        try:
            header = self._recv_exact(2)
        except TimeoutError:
            return None
        first, second = header
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload

    def _recv_exact(self, length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            try:
                chunk = self.sock.recv(length - len(data))
            except socket.timeout as exc:
                raise TimeoutError("websocket read timed out") from exc
            if not chunk:
                raise WebSocketProtocolError("websocket connection closed")
            data.extend(chunk)
        return bytes(data)


@dataclass
class ClobMarketOrderBook:
    market: MarketRound
    token_to_side: dict[str, str]
    quotes: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def for_market(cls, market: MarketRound) -> "ClobMarketOrderBook":
        return cls(
            market=market,
            token_to_side={
                str(market.up_token): "Up",
                str(market.down_token): "Down",
            },
        )

    def apply_payload(self, payload: Any, now: float | None = None) -> dict[str, dict[str, Any]]:
        now = time.time() if now is None else now
        changed: dict[str, dict[str, Any]] = {}
        messages = payload if isinstance(payload, list) else [payload]
        for message in messages:
            if not isinstance(message, dict):
                continue
            event_type = str(message.get("event_type") or "")
            if event_type == "book":
                side = self._side_for_asset(message.get("asset_id"))
                if side and self._apply_book(side, message, now):
                    changed[side] = dict(self.quotes[side])
            elif event_type == "price_change":
                for row in message.get("price_changes") or []:
                    if not isinstance(row, dict):
                        continue
                    side = self._side_for_asset(row.get("asset_id"))
                    if side and self._apply_price_change(side, row, message, now):
                        changed[side] = dict(self.quotes[side])
            elif event_type == "best_bid_ask":
                side = self._side_for_asset(message.get("asset_id"))
                if side and self._apply_best_bid_ask(side, message, now):
                    changed[side] = dict(self.quotes[side])
        return changed

    def _side_for_asset(self, asset_id: Any) -> str | None:
        return self.token_to_side.get(str(asset_id or ""))

    def _apply_book(self, side: str, message: dict[str, Any], now: float) -> bool:
        bids = _normalize_levels(message.get("bids"), reverse=True)
        asks = _normalize_levels(message.get("asks"), reverse=False)
        self.quotes[side] = _quote_from_levels(
            token_id=str(message.get("asset_id") or ""),
            side=side,
            bids=bids,
            asks=asks,
            updated_at_ms=_received_timestamp_ms(now),
            source="clob-ws-book",
        )
        _apply_clob_quote_timestamps(self.quotes[side], message, now)
        return True

    def _apply_price_change(
        self,
        side: str,
        row: dict[str, Any],
        message: dict[str, Any],
        now: float,
    ) -> bool:
        quote = dict(self.quotes.get(side) or _empty_quote(str(row.get("asset_id") or ""), side))
        book_side = str(row.get("side") or "").upper()
        level_key = "bids" if book_side == "BUY" else "asks" if book_side == "SELL" else ""
        if level_key:
            reverse = level_key == "bids"
            levels = _upsert_level(quote.get(level_key), row.get("price"), row.get("size"), reverse=reverse)
            quote[level_key] = levels
            quote = _with_best_levels(quote)
        best_bid = _maybe_float(row.get("best_bid"))
        best_ask = _maybe_float(row.get("best_ask"))
        if best_bid is not None:
            quote["best_bid"] = best_bid
        if best_ask is not None:
            quote["best_ask"] = best_ask
        _apply_clob_quote_timestamps(quote, message, now)
        quote["source"] = "clob-ws-price-change"
        self.quotes[side] = quote
        return True

    def _apply_best_bid_ask(self, side: str, message: dict[str, Any], now: float) -> bool:
        quote = dict(self.quotes.get(side) or _empty_quote(str(message.get("asset_id") or ""), side))
        best_bid = _maybe_float(message.get("best_bid"))
        best_ask = _maybe_float(message.get("best_ask"))
        if best_bid is not None:
            quote["best_bid"] = best_bid
        if best_ask is not None:
            quote["best_ask"] = best_ask
        _apply_clob_quote_timestamps(quote, message, now)
        quote["source"] = "clob-ws-best"
        self.quotes[side] = quote
        return True


class ClobMarketWebSocketFeed:
    def __init__(self, url: str = CLOB_MARKET_WS_URL, timeout_seconds: float = 5.0) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        stop_event: Any,
        market_provider: Callable[[], MarketRound | None],
        quote_callback: Callable[[MarketRound, dict[str, dict[str, Any]], dict[str, Any]], None],
        status_callback: Callable[[dict[str, Any]], None],
    ) -> None:
        backoff = CLOB_WS_RECONNECT_INITIAL_SECONDS
        while not stop_event.is_set():
            market = market_provider()
            if not _market_has_tokens(market) or (market and market.ends_at <= time.time() + 0.5):
                status_callback({"state": "waiting_market", "at": time.time()})
                stop_event.wait(0.5)
                continue
            assert market is not None
            try:
                self._run_market(stop_event, market, market_provider, quote_callback, status_callback)
                backoff = CLOB_WS_RECONNECT_INITIAL_SECONDS
            except Exception as exc:  # noqa: BLE001 - caller reports status and reconnects.
                status_callback(
                    {
                        "state": "error",
                        "market": market.round_id,
                        "error": f"{type(exc).__name__}: {exc}",
                        "at": time.time(),
                    }
                )
                stop_event.wait(backoff)
                backoff = min(CLOB_WS_RECONNECT_MAX_SECONDS, backoff * 1.8)

    def _run_market(
        self,
        stop_event: Any,
        market: MarketRound,
        market_provider: Callable[[], MarketRound | None],
        quote_callback: Callable[[MarketRound, dict[str, dict[str, Any]], dict[str, Any]], None],
        status_callback: Callable[[dict[str, Any]], None],
    ) -> None:
        status_callback({"state": "connecting", "market": market.round_id, "at": time.time()})
        ws = _StdlibWebSocket.connect(self.url, self.timeout_seconds)
        try:
            book = ClobMarketOrderBook.for_market(market)
            subscription = {
                "assets_ids": [market.up_token, market.down_token],
                "type": "market",
                "custom_feature_enabled": True,
            }
            ws.send_text(json.dumps(subscription, separators=(",", ":")))
            ws.send_text("PING")
            last_ping = time.time()
            status_callback(
                {
                    "state": "connected",
                    "market": market.round_id,
                    "asset_count": 2,
                    "at": last_ping,
                }
            )
            while not stop_event.is_set():
                current_market = market_provider()
                if not _same_market_subscription(current_market, market) or market.ends_at <= time.time() + 0.2:
                    status_callback({"state": "resubscribe", "market": market.round_id, "at": time.time()})
                    return
                now = time.time()
                if now - last_ping >= CLOB_WS_PING_SECONDS:
                    ws.send_text("PING")
                    last_ping = now
                text = ws.read_text(CLOB_WS_READ_TIMEOUT_SECONDS)
                if text is None:
                    continue
                if text in {"PING", "PONG", ""}:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                changed = book.apply_payload(payload, time.time())
                if changed:
                    quote_callback(
                        market,
                        changed,
                        {
                            "state": "message",
                            "market": market.round_id,
                            "event_type": _payload_event_type(payload),
                            "at": time.time(),
                        },
                    )
        finally:
            ws.close()


class RtdsChainlinkWebSocketFeed:
    def __init__(self, url: str = RTDS_WS_URL, timeout_seconds: float = 5.0) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self._last_unparsed_log_at = 0.0

    def run(
        self,
        stop_event: Any,
        price_callback: Callable[[dict[str, Any], dict[str, Any]], None],
        status_callback: Callable[[dict[str, Any]], None],
    ) -> None:
        backoff = CLOB_WS_RECONNECT_INITIAL_SECONDS
        while not stop_event.is_set():
            try:
                self._run_once(stop_event, price_callback, status_callback)
                backoff = CLOB_WS_RECONNECT_INITIAL_SECONDS
            except Exception as exc:  # noqa: BLE001 - caller reports status and reconnects.
                status_callback(
                    {
                        "state": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                        "at": time.time(),
                    }
                )
                stop_event.wait(backoff)
                backoff = min(CLOB_WS_RECONNECT_MAX_SECONDS, backoff * 1.8)

    def _run_once(
        self,
        stop_event: Any,
        price_callback: Callable[[dict[str, Any], dict[str, Any]], None],
        status_callback: Callable[[dict[str, Any]], None],
    ) -> None:
        status_callback({"state": "connecting", "at": time.time()})
        ws = _StdlibWebSocket.connect(self.url, self.timeout_seconds)
        try:
            subscription = {
                "action": "subscribe",
                "subscriptions": [
                    {
                        "topic": "crypto_prices_chainlink",
                        "type": "*",
                        "filters": "{\"symbol\":\"btc/usd\"}",
                    },
                    {
                        "topic": "crypto_prices",
                        "type": "update",
                        # Polymarket RTDS 的 btcusdt filter 当前只稳定返回首批，本地过滤能拿到持续 update。
                        "filters": "",
                    }
                ],
            }
            ws.send_text(json.dumps(subscription, separators=(",", ":")))
            ws.send_text("PING")
            last_ping = time.time()
            status_callback({"state": "connected", "topic": "crypto_prices_chainlink,crypto_prices", "at": last_ping})
            while not stop_event.is_set():
                now = time.time()
                if now - last_ping >= RTDS_WS_PING_SECONDS:
                    ws.send_text("PING")
                    last_ping = now
                text = ws.read_text(CLOB_WS_READ_TIMEOUT_SECONDS)
                if text is None:
                    continue
                if text in {"PING", "PONG", ""}:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                message_at = time.time()
                tick = rtds_chainlink_price_from_payload(payload, message_at)
                if tick:
                    price_callback(
                        tick,
                        {
                            "state": "message",
                            "topic": _rtds_payload_topic(payload) or "crypto_prices_chainlink",
                            "at": time.time(),
                        },
                    )
                    continue
                spot_tick = rtds_spot_price_from_payload(payload, message_at)
                if spot_tick:
                    price_callback(
                        spot_tick,
                        {
                            "state": "message",
                            "topic": _rtds_payload_topic(payload) or "crypto_prices",
                            "at": time.time(),
                        },
                    )
                else:
                    self._log_unparsed_payload(payload)
        finally:
            ws.close()

    def _log_unparsed_payload(self, payload: Any) -> None:
        now = time.time()
        if now - self._last_unparsed_log_at < RTDS_UNPARSED_LOG_INTERVAL_SECONDS:
            return
        if isinstance(payload, dict) and _is_expected_ignored_rtds_crypto_payload(payload):
            return
        rows = _rtds_price_rows(payload) if isinstance(payload, dict) else []
        if not rows:
            return
        self._last_unparsed_log_at = now
        # RTDS 首批消息偶尔缺少 topic/symbol，保留摘要帮助定位是无推送还是解析丢弃。
        logger.warning(
            "RTDS Chainlink 消息未解析 rows=%s topic=%s symbol=%s keys=%s",
            len(rows),
            payload.get("topic") if isinstance(payload, dict) else None,
            payload.get("symbol") if isinstance(payload, dict) else None,
            sorted(payload.keys()) if isinstance(payload, dict) else [],
        )


class SpotPriceWebSocketFeed:
    def __init__(self, source: str, url: str | None = None, timeout_seconds: float = 5.0) -> None:
        self.source = source
        self.url = url or _spot_ws_url(source)
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        stop_event: Any,
        price_callback: Callable[[dict[str, Any], dict[str, Any]], None],
        status_callback: Callable[[dict[str, Any]], None],
    ) -> None:
        backoff = CLOB_WS_RECONNECT_INITIAL_SECONDS
        while not stop_event.is_set():
            try:
                self._run_once(stop_event, price_callback, status_callback)
                backoff = CLOB_WS_RECONNECT_INITIAL_SECONDS
            except Exception as exc:  # noqa: BLE001 - 行情线程必须自动重连，不能影响实盘主循环。
                status_callback(
                    {
                        "state": "error",
                        "source": self.source,
                        "error": f"{type(exc).__name__}: {exc}",
                        "at": time.time(),
                    }
                )
                stop_event.wait(backoff)
                backoff = min(CLOB_WS_RECONNECT_MAX_SECONDS, backoff * 1.8)

    def _run_once(
        self,
        stop_event: Any,
        price_callback: Callable[[dict[str, Any], dict[str, Any]], None],
        status_callback: Callable[[dict[str, Any]], None],
    ) -> None:
        status_callback({"state": "connecting", "source": self.source, "at": time.time()})
        ws = _StdlibWebSocket.connect(self.url, self.timeout_seconds)
        try:
            if self.source == SPOT_WS_SOURCE_OKX:
                ws.send_text(
                    json.dumps(
                        {"op": "subscribe", "args": [{"channel": "tickers", "instId": "BTC-USDT"}]},
                        separators=(",", ":"),
                    )
                )
            last_ping = time.time()
            status_callback({"state": "connected", "source": self.source, "at": last_ping})
            while not stop_event.is_set():
                now = time.time()
                if self.source == SPOT_WS_SOURCE_OKX and now - last_ping >= SPOT_WS_PING_SECONDS:
                    ws.send_text("ping")
                    last_ping = now
                text = ws.read_text(CLOB_WS_READ_TIMEOUT_SECONDS)
                if text is None:
                    continue
                if text in {"PING", "PONG", "pong", ""}:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                tick = spot_price_from_payload(self.source, payload, time.time())
                if tick:
                    price_callback(
                        tick,
                        {
                            "state": "message",
                            "source": self.source,
                            "at": time.time(),
                        },
                    )
        finally:
            ws.close()


def spot_price_from_payload(source: str, payload: Any, now: float | None = None) -> dict[str, Any] | None:
    now = time.time() if now is None else now
    if source == SPOT_WS_SOURCE_OKX:
        return okx_spot_price_from_payload(payload, now)
    if source == SPOT_WS_SOURCE_BINANCE:
        return binance_spot_price_from_payload(payload, now)
    return None


def okx_spot_price_from_payload(payload: Any, now: float | None = None) -> dict[str, Any] | None:
    now = time.time() if now is None else now
    received_ms = int(now * 1000)
    messages = payload if isinstance(payload, list) else [payload]
    for message in messages:
        if not isinstance(message, dict):
            continue
        rows = message.get("data")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            arg = message.get("arg") if isinstance(message.get("arg"), dict) else {}
            inst_id = str(row.get("instId") or arg.get("instId") or "").upper()
            if inst_id and inst_id != "BTC-USDT":
                continue
            price = _maybe_float(row.get("last"))
            if price is None or price <= 0:
                continue
            exchange_updated_ms = _normalize_timestamp_ms(row.get("ts"), now)
            return {
                "okx": price,
                # 策略 freshness 使用本机收到时间，避免交易所事件时间抖动误判过期。
                "okx_updated_ms": received_ms,
                "okx_received_ms": received_ms,
                "okx_exchange_updated_ms": exchange_updated_ms,
                "okx_source": "okx-spot-ws",
            }
    return None


def binance_spot_price_from_payload(payload: Any, now: float | None = None) -> dict[str, Any] | None:
    now = time.time() if now is None else now
    received_ms = int(now * 1000)
    messages = payload if isinstance(payload, list) else [payload]
    for message in messages:
        if not isinstance(message, dict):
            continue
        symbol = str(message.get("s") or "").upper()
        if symbol and symbol != "BTCUSDT":
            continue
        price = _maybe_float(message.get("c") or message.get("lastPrice"))
        if price is None or price <= 0:
            continue
        exchange_updated_ms = _normalize_timestamp_ms(message.get("E"), now)
        return {
            "binance_market": price,
            # 策略 freshness 使用本机收到时间，事件时间保留给诊断。
            "binance_market_updated_ms": received_ms,
            "binance_market_received_ms": received_ms,
            "binance_market_exchange_updated_ms": exchange_updated_ms,
            "binance_market_source": "binance-spot-ws",
        }
    return None


def rtds_chainlink_price_from_payload(payload: Any, now: float | None = None) -> dict[str, Any] | None:
    now = time.time() if now is None else now
    messages = payload if isinstance(payload, list) else [payload]
    latest_tick: dict[str, Any] | None = None
    for message in messages:
        if not isinstance(message, dict):
            continue
        topic = str(message.get("topic") or "")
        rows = _rtds_price_rows(message)
        inferred_chainlink = _is_untagged_rtds_chainlink_payload(message, rows)
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        payload_symbol = str(payload.get("symbol") or "").strip().lower()
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or message.get("symbol") or payload_symbol or "").strip().lower()
            if topic != "crypto_prices_chainlink" and not _is_rtds_chainlink_btc_symbol(symbol) and not inferred_chainlink:
                continue
            value = _maybe_float(row.get("value") or row.get("price"))
            if value is None or value <= 0:
                continue
            updated_ms = _normalize_timestamp_ms(row.get("timestamp") or message.get("timestamp"), now)
            tick = {
                "chainlink": value,
                "chainlink_updated_ms": updated_ms,
                "source": "polymarket-rtds-chainlink",
            }
            if latest_tick is None or updated_ms >= int(latest_tick["chainlink_updated_ms"]):
                latest_tick = tick
    return latest_tick


def rtds_spot_price_from_payload(payload: Any, now: float | None = None) -> dict[str, Any] | None:
    now = time.time() if now is None else now
    received_ms = int(now * 1000)
    messages = payload if isinstance(payload, list) else [payload]
    latest_tick: dict[str, Any] | None = None
    for message in messages:
        if not isinstance(message, dict):
            continue
        topic = str(message.get("topic") or "")
        if topic and topic != "crypto_prices":
            continue
        payload_obj = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        payload_symbol = str(payload_obj.get("symbol") or "").strip().lower()
        for row in _rtds_price_rows(message):
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or message.get("symbol") or payload_symbol or "").strip().lower()
            if not _is_rtds_crypto_btc_symbol(symbol):
                continue
            value = _maybe_float(row.get("value") or row.get("price"))
            if value is None or value <= 0:
                continue
            exchange_updated_ms = _normalize_timestamp_ms(row.get("timestamp") or message.get("timestamp"), now)
            tick = {
                "binance_market": value,
                # 策略 freshness 使用收到时间，RTDS 原始时间保留给诊断。
                "binance_market_updated_ms": received_ms,
                "binance_market_received_ms": received_ms,
                "binance_market_exchange_updated_ms": exchange_updated_ms,
                "binance_market_source": "polymarket-rtds-crypto-prices",
            }
            if latest_tick is None or exchange_updated_ms >= int(latest_tick["binance_market_exchange_updated_ms"]):
                latest_tick = tick
    return latest_tick


def _is_untagged_rtds_chainlink_payload(message: dict[str, Any], rows: list[dict[str, Any]]) -> bool:
    if message.get("topic") or message.get("symbol"):
        return False
    if "payload" not in message:
        return False
    if not rows:
        return False
    # Polymarket RTDS 的 crypto_prices_chainlink 初始批次可能只返回 timestamp/value 序列。
    return all(_maybe_float(row.get("value") or row.get("price")) is not None for row in rows)


def _rtds_symbol_key(symbol: str) -> str:
    return "".join(ch for ch in str(symbol or "").lower() if ch.isalnum())


def _is_rtds_chainlink_btc_symbol(symbol: str) -> bool:
    return _rtds_symbol_key(symbol) in RTDS_CHAINLINK_BTC_SYMBOL_KEYS


def _is_rtds_crypto_btc_symbol(symbol: str) -> bool:
    return _rtds_symbol_key(symbol) in RTDS_CRYPTO_BTC_SYMBOL_KEYS


def _rtds_payload_topic(payload: Any) -> str | None:
    if isinstance(payload, dict) and payload.get("topic"):
        return str(payload.get("topic"))
    return None


def _is_expected_ignored_rtds_crypto_payload(message: dict[str, Any]) -> bool:
    if str(message.get("topic") or "") != "crypto_prices":
        return False
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    payload_symbol = str(payload.get("symbol") or "").strip().lower()
    symbols = {
        str(row.get("symbol") or message.get("symbol") or payload_symbol or "").strip().lower()
        for row in _rtds_price_rows(message)
        if isinstance(row, dict)
    }
    symbols.discard("")
    if not symbols:
        return False
    return not any(_is_rtds_crypto_btc_symbol(symbol) or _is_rtds_chainlink_btc_symbol(symbol) for symbol in symbols)


def _rtds_price_rows(message: dict[str, Any]) -> list[dict[str, Any]]:
    payload = message.get("payload")
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            return [data]
        return [payload]
    data = message.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [data]
    return [message]


def _normalize_timestamp_ms(value: Any, now: float) -> int:
    raw = _maybe_float(value)
    if raw is None or raw <= 0:
        return int(now * 1000)
    if raw < 10_000_000_000:
        return int(raw * 1000)
    return int(raw)


def _spot_ws_url(source: str) -> str:
    if source == SPOT_WS_SOURCE_OKX:
        return OKX_SPOT_WS_URL
    if source == SPOT_WS_SOURCE_BINANCE:
        return BINANCE_SPOT_WS_URL
    raise ValueError(f"unsupported spot websocket source: {source}")


def _market_has_tokens(market: MarketRound | None) -> bool:
    return bool(market and market.up_token and market.down_token)


def _same_market_subscription(current: MarketRound | None, subscribed: MarketRound) -> bool:
    return bool(
        current
        and current.round_id == subscribed.round_id
        and str(current.up_token) == str(subscribed.up_token)
        and str(current.down_token) == str(subscribed.down_token)
    )


def _payload_event_type(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("event_type") or "")
    if isinstance(payload, list):
        events = [str(row.get("event_type") or "") for row in payload if isinstance(row, dict)]
        return ",".join(sorted({event for event in events if event}))
    return ""


def _empty_quote(token_id: str, side: str) -> dict[str, Any]:
    return {
        "token_id": token_id,
        "outcome": side,
        "best_bid": None,
        "best_ask": None,
        "bid_size": None,
        "ask_size": None,
        "bids": [],
        "asks": [],
        "source": "clob-ws",
    }


def _quote_from_levels(
    *,
    token_id: str,
    side: str,
    bids: list[dict[str, float]],
    asks: list[dict[str, float]],
    updated_at_ms: int,
    source: str,
) -> dict[str, Any]:
    quote = _empty_quote(token_id, side)
    quote["bids"] = bids
    quote["asks"] = asks
    quote["updated_at_ms"] = updated_at_ms
    quote["source"] = source
    return _with_best_levels(quote)


def _with_best_levels(quote: dict[str, Any]) -> dict[str, Any]:
    bids = _normalize_levels(quote.get("bids"), reverse=True)
    asks = _normalize_levels(quote.get("asks"), reverse=False)
    quote["bids"] = bids
    quote["asks"] = asks
    best_bid = bids[0] if bids else {}
    best_ask = asks[0] if asks else {}
    quote["best_bid"] = _maybe_float(best_bid.get("price"))
    quote["bid_size"] = _maybe_float(best_bid.get("size"))
    quote["best_ask"] = _maybe_float(best_ask.get("price"))
    quote["ask_size"] = _maybe_float(best_ask.get("size"))
    return quote


def _normalize_levels(levels: Any, *, reverse: bool) -> list[dict[str, float]]:
    normalized: list[dict[str, float]] = []
    for level in levels or []:
        if not isinstance(level, dict):
            continue
        price = _maybe_float(level.get("price"))
        size = _maybe_float(level.get("size"))
        if price is None or size is None or price <= 0 or price >= 1 or size <= 0:
            continue
        normalized.append({"price": price, "size": size})
    normalized.sort(key=lambda row: row["price"], reverse=reverse)
    return normalized[:MAX_CLOB_WS_LEVELS]


def _upsert_level(levels: Any, price_value: Any, size_value: Any, *, reverse: bool) -> list[dict[str, float]]:
    price = _maybe_float(price_value)
    size = _maybe_float(size_value)
    current = _normalize_levels(levels, reverse=reverse)
    if price is None or price <= 0 or price >= 1:
        return current
    next_levels = [level for level in current if abs(float(level["price"]) - price) > 0.000000001]
    if size is not None and size > 0:
        next_levels.append({"price": price, "size": size})
    next_levels.sort(key=lambda row: row["price"], reverse=reverse)
    return next_levels[:MAX_CLOB_WS_LEVELS]


def _event_timestamp_ms(message: dict[str, Any], now: float) -> int:
    value = _maybe_int(message.get("timestamp"))
    if value and value > 0:
        return value
    return int(now * 1000)


def _received_timestamp_ms(now: float) -> int:
    return int(now * 1000)


def _apply_clob_quote_timestamps(quote: dict[str, Any], message: dict[str, Any], now: float) -> None:
    received_ms = _received_timestamp_ms(now)
    # 策略 freshness 使用本机收到时间，CLOB 事件时间单独保留给诊断。
    quote["updated_at_ms"] = received_ms
    quote["clob_received_ms"] = received_ms
    quote["clob_event_updated_ms"] = _event_timestamp_ms(message, now)


def _maybe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
