"""Buffered, bounded WebSocket reader used by the managed-live market feed only."""
import base64
import hashlib
import os
import socket
import ssl
import struct
import time
import urllib.parse

from .clob_ws import _StdlibWebSocket, WebSocketProtocolError, _WEBSOCKET_GUID


class BufferedWebSocket(_StdlibWebSocket):
    MAX_MESSAGE=8*1024*1024

    def __init__(self,sock,initial=b""):
        super().__init__(sock)
        self._buffer=bytearray(initial);self._offset=0
        self._fragments=bytearray();self._fragment_kind=None

    @classmethod
    def connect(cls,url,timeout_seconds):
        parsed=urllib.parse.urlparse(url)
        if parsed.scheme!="wss" or not parsed.hostname:raise ValueError("only wss:// endpoints are supported")
        host=parsed.hostname;port=parsed.port or 443
        path=parsed.path or "/"
        if parsed.query:path+="?"+parsed.query
        raw=socket.create_connection((host,port),timeout=timeout_seconds);sock=None
        try:
            sock=ssl.create_default_context().wrap_socket(raw,server_hostname=host)
            sock.settimeout(timeout_seconds);key=base64.b64encode(os.urandom(16)).decode("ascii")
            authority=host if port==443 else f"{host}:{port}"
            request=(f"GET {path} HTTP/1.1\r\nHost: {authority}\r\nUpgrade: websocket\r\n"
                     f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                     "User-Agent: polybot2other/live-feed\r\n\r\n")
            sock.sendall(request.encode("ascii"))
            received=cls._read_http_header(sock)
            header,tail=received.split(b"\r\n\r\n",1)
            cls._validate_handshake(header,key)
            headers={}
            for line in header.decode("iso-8859-1").split("\r\n")[1:]:
                if ":" in line:
                    name,value=line.split(":",1);headers[name.strip().lower()]=value.strip()
            expected=base64.b64encode(hashlib.sha1((key+_WEBSOCKET_GUID).encode()).digest()).decode()
            if headers.get("sec-websocket-accept")!=expected:raise WebSocketProtocolError("invalid handshake accept")
            if headers.get("upgrade","").lower()!="websocket":raise WebSocketProtocolError("missing websocket upgrade")
            return cls(sock,tail)
        except Exception:
            (sock or raw).close();raise

    def _ensure(self,length,deadline):
        while len(self._buffer)-self._offset<length:
            remaining=deadline-time.monotonic()
            if remaining<=0:return False
            if self._offset:
                del self._buffer[:self._offset];self._offset=0
            self.sock.settimeout(remaining)
            try:chunk=self.sock.recv(65536)
            except (TimeoutError,socket.timeout):return False
            if not chunk:raise WebSocketProtocolError("websocket connection closed")
            self._buffer.extend(chunk)
        return True

    def _frame(self,deadline):
        # Consume nothing until a complete frame is buffered. A partial header or
        # payload survives a read deadline, as do fragmented text messages.
        if not self._ensure(2,deadline):return None
        first,second=self._buffer[self._offset:self._offset+2]
        if first&0x70:raise WebSocketProtocolError("unsupported websocket extension")
        fin=bool(first&0x80);opcode=first&15;masked=bool(second&128);size=second&127;header=2
        if size in (126,127):
            extra=2 if size==126 else 8
            if not self._ensure(header+extra,deadline):return None
            size=struct.unpack("!H" if extra==2 else "!Q",self._buffer[self._offset+2:self._offset+2+extra])[0]
            header+=extra
        if size>self.MAX_MESSAGE:raise WebSocketProtocolError("websocket message too large")
        if opcode>=8 and (not fin or size>125):raise WebSocketProtocolError("invalid control frame")
        total=header+(4 if masked else 0)+size
        if not self._ensure(total,deadline):return None
        start=self._offset+header
        mask=bytes(self._buffer[start:start+4]) if masked else b""
        if masked:start+=4
        payload=bytes(self._buffer[start:start+size]);self._offset+=total
        if masked:payload=bytes(b^mask[i%4] for i,b in enumerate(payload))
        return fin,opcode,payload

    def read_text(self,timeout_seconds=1):
        deadline=time.monotonic()+timeout_seconds
        while True:
            frame=self._frame(deadline)
            if frame is None:return None
            fin,opcode,payload=frame
            if opcode==8:raise WebSocketProtocolError("websocket closed by server")
            if opcode==9:self._send_pong(payload);continue
            if opcode==10:continue
            if opcode in (1,2):
                if self._fragment_kind is not None:raise WebSocketProtocolError("interleaved data messages")
                if fin:
                    if opcode==1:return payload.decode("utf-8")
                    continue
                self._fragment_kind=opcode;self._fragments=bytearray(payload)
            elif opcode==0 and self._fragment_kind is not None:
                self._fragments.extend(payload)
                if len(self._fragments)>self.MAX_MESSAGE:raise WebSocketProtocolError("websocket message too large")
                if fin:
                    kind=self._fragment_kind;self._fragment_kind=None
                    completed=bytes(self._fragments);self._fragments.clear()
                    if kind==1:return completed.decode("utf-8")
            else:raise WebSocketProtocolError("invalid websocket opcode")
