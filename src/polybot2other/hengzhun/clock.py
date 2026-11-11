"""衡准专用只读时钟证据；不校准操作系统，不影响其他策略。

NTP 包校验和 offset/delay 定义依据 RFC 5905 §§7–8。
https://www.rfc-editor.org/rfc/rfc5905.html
普通 NTP 不提供密码学认证，报告明确保留这一信任边界。
"""
from __future__ import annotations

import hashlib
import math
import socket
import struct
import threading
import time
import uuid

NTP_UNIX_EPOCH = 2_208_988_800
NTP_ERA = 1 << 32


def _encode_timestamp(unix_seconds):
    full = unix_seconds+NTP_UNIX_EPOCH
    seconds = math.floor(full)
    fraction = int((full-seconds)*NTP_ERA)
    return struct.pack("!II", seconds % NTP_ERA, fraction)


def _decode_timestamp(raw, reference_unix):
    seconds, fraction = struct.unpack("!II",raw)
    if not seconds and not fraction: raise ValueError("NTP_ZERO_TIMESTAMP")
    reference = reference_unix+NTP_UNIX_EPOCH
    era = math.floor(reference/NTP_ERA)
    full = min((seconds+(era+j)*NTP_ERA+fraction/NTP_ERA for j in (-1,0,1)),key=lambda x:abs(x-reference))
    return full-NTP_UNIX_EPOCH


def parse_response(packet, request_transmit, *, sent_at, received_at, monotonic_rtt,
                   server, address, jump_tolerance_ms=10):
    """仅解析给定 NTP 响应；源地址由 connect 后的 UDP socket 验证。"""
    if len(packet) < 48: raise ValueError("NTP_TRUNCATED_RESPONSE")
    leap, version, mode = packet[0] >> 6, (packet[0] >> 3) & 7, packet[0] & 7
    stratum = packet[1]
    if leap == 3 or not 1 <= stratum <= 15: raise ValueError("NTP_UNSYNCHRONIZED_OR_KOD")
    if version not in (3,4) or mode != 4: raise ValueError("NTP_INVALID_VERSION_OR_MODE")
    if packet[24:32] != request_transmit: raise ValueError("NTP_ORIGIN_MISMATCH")
    if not all(math.isfinite(x) for x in (sent_at,received_at,monotonic_rtt)) or monotonic_rtt < 0:
        raise ValueError("NTP_LOCAL_TIME_INVALID")
    if abs((received_at-sent_at)-monotonic_rtt)*1000 > jump_tolerance_ms:
        raise ValueError("CLOCK_JUMP_DURING_PROBE")
    t2 = _decode_timestamp(packet[32:40],received_at)
    t3 = _decode_timestamp(packet[40:48],received_at)
    reference = _decode_timestamp(packet[16:24],received_at)
    if t3 < t2 or reference > t3: raise ValueError("NTP_TIMESTAMP_ORDER_INVALID")
    server_processing = t3-t2
    delay = monotonic_rtt-server_processing
    if delay < -.001: raise ValueError("NTP_NEGATIVE_NETWORK_DELAY")
    precision_exp = struct.unpack("!b",packet[3:4])[0]
    if not -50 <= precision_exp <= 20: raise ValueError("NTP_PRECISION_INVALID")
    root_delay = struct.unpack("!i",packet[4:8])[0]/65536.
    root_dispersion = struct.unpack("!I",packet[8:12])[0]/65536.
    offset = ((t2-sent_at)+(t3-received_at))/2
    # 使用完整观测 RTT/2 比扣除服务器处理时间后的 network_delay/2 更保守。
    uncertainty = monotonic_rtt/2+root_dispersion+max(0.,root_delay)/2+2.**precision_exp+.001
    return {"status":"VERIFIED","server":server,"address":str(address),"checked_at":int(received_at*1000),
            "offset_ms":offset*1000,"offset_bound_ms":(abs(offset)+uncertainty)*1000,
            "round_trip_ms":monotonic_rtt*1000,"network_delay_ms":max(0.,delay)*1000,
            "root_delay_ms":root_delay*1000,"root_dispersion_ms":root_dispersion*1000,
            "precision_ms":2.**precision_exp*1000,"stratum":stratum,"leap_indicator":leap,
            "source":"NTP_READ_ONLY","authenticated":False,"trust_basis":"CONFIGURED_NTP_SERVER",
            "packet_sha256":hashlib.sha256(packet).hexdigest(),"system_clock_modified":False}


def probe(server="time.apple.com",timeout=2,*,wall_clock=None,monotonic=None,socket_factory=None,resolver=None):
    """查询配置的 NTP 服务器。只发时间请求；无凭据、无写时钟操作。"""
    wall_clock = wall_clock or time.time; monotonic = monotonic or time.monotonic
    socket_factory = socket_factory or socket.socket; resolver = resolver or socket.getaddrinfo
    if not isinstance(server,str) or not server or not isinstance(timeout,(int,float)) or not 0 < timeout <= 30:
        raise ValueError("NTP_PROBE_CONFIG_INVALID")
    candidates = resolver(server,123,type=socket.SOCK_DGRAM)
    if not candidates: raise OSError("NTP_DNS_NO_ADDRESS")
    family, socktype, protocol, _, address = candidates[0]
    with socket_factory(family,socktype,protocol) as connection:
        connection.settimeout(timeout)
        connection.connect(address)
        sent_at = wall_clock(); start = monotonic()
        transmit = _encode_timestamp(sent_at)
        request = bytearray(48); request[0] = 0x23; request[40:48] = transmit
        connection.send(bytes(request))
        packet = connection.recv(4096)
        end = monotonic(); received_at = wall_clock()
    return parse_response(packet,transmit,sent_at=sent_at,received_at=received_at,
                          monotonic_rtt=end-start,server=server,address=address)


class ClockMonitor:
    def __init__(self,server="time.apple.com",*,offset_limit_ms=250,max_age_ms=120000,
                 drift_ppm=15,jump_tolerance_ms=10,wall_clock=None,monotonic=None,probe_fn=None):
        self.server = server; self.offset_limit_ms = offset_limit_ms; self.max_age_ms = max_age_ms
        self.drift_ppm = drift_ppm; self.jump_tolerance_ms = jump_tolerance_ms
        self.wall_clock = wall_clock or time.time; self.monotonic = monotonic or time.monotonic
        self.probe_fn = probe_fn or probe; self.boot_id = uuid.uuid4().hex
        self.lock = threading.RLock(); self.evidence = None; self.error = "NO_CLOCK_EVIDENCE"
        self.last_jump = None; self._last_wall = None; self._last_mono = None; self._small_adjustments_ms = 0.
        self._anchor_wall = None; self._anchor_mono = None

    def probe(self,server=None,timeout=2):
        server = server or self.server
        try:
            result = self.probe_fn(server=server,timeout=timeout,wall_clock=self.wall_clock,monotonic=self.monotonic)
            bound = result.get("offset_bound_ms")
            if (result.get("status") != "VERIFIED" or not isinstance(bound,(int,float))
                    or isinstance(bound,bool) or not math.isfinite(bound) or bound < 0
                    or not isinstance(result.get("checked_at"),int)):
                raise ValueError("NTP_PROBE_EVIDENCE_INVALID")
            with self.lock:
                self.evidence = dict(result); self.error = None
                self._last_wall = self.wall_clock()*1000; self._last_mono = self.monotonic()*1000
                self._anchor_wall = self._last_wall; self._anchor_mono = self._last_mono
                self._small_adjustments_ms = 0.
        except Exception as exc:
            with self.lock:
                self.evidence = None
                self.error = str(exc)[:120] if isinstance(exc,ValueError) else type(exc).__name__
        return self.snapshot()

    def snapshot(self,now_ms=None):
        now = int(self.wall_clock()*1000) if now_ms is None else now_ms
        mono = self.monotonic()*1000
        with self.lock:
            if self._last_wall is not None:
                change = (now-self._last_wall)-(mono-self._last_mono)
                if mono < self._last_mono or abs(change) > self.jump_tolerance_ms:
                    self.last_jump = {"at":now,"delta_ms":change,"boot_id":self.boot_id}
                    self.evidence = None; self.error = "CLOCK_JUMP_DETECTED"
                else:
                    # 对同一次证据锚点检查累计偏差；不累加每次整数毫秒取整噪声。
                    if self._anchor_wall is not None:
                        drift=abs((now-self._anchor_wall)-(mono-self._anchor_mono))
                        self._small_adjustments_ms=max(self._small_adjustments_ms,drift)
            self._last_wall = now; self._last_mono = mono
            base = {"boot_id":self.boot_id,"server":self.server,"last_jump":self.last_jump,
                    "offset_bound_ms":None,"checked_at":None,"status":"CLOCK_UNVERIFIED",
                    "reason":self.error,"system_clock_modified":False}
            if not self.evidence: return base
            age = now-self.evidence["checked_at"]
            if not 0 <= age <= self.max_age_ms:
                return {**base,"checked_at":self.evidence["checked_at"],"reason":"CLOCK_EVIDENCE_STALE",
                        "evidence":dict(self.evidence)}
            bound = self.evidence["offset_bound_ms"]+age*self.drift_ppm/1_000_000+self._small_adjustments_ms
            return {**base,"offset_bound_ms":bound,"checked_at":self.evidence["checked_at"],
                    "status":"VERIFIED" if bound <= self.offset_limit_ms else "CLOCK_OFFSET_EXCEEDED",
                    "reason":None if bound <= self.offset_limit_ms else "CLOCK_OFFSET_EXCEEDED",
                    "evidence":dict(self.evidence)}
