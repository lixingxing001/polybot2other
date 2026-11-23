#!/usr/bin/env python3
"""Loopback-only macOS access to the two registered EdgePulse SSH forwards."""
import concurrent.futures
import contextlib
import fcntl
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

SSH = "/usr/bin/ssh"
PORT = 18790
BASE = Path(__file__).resolve().parent
SUPPORT = Path.home() / "Library/Application Support/EdgePulse"
SERVICE = "edgepulse-server-access-v1"
USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")
TUNNELS = (
    ("dashboard", "服务器管理面板", 18791, 8791, "/#bot", "/api/managed-live"),
    ("trials", "Paper 策略对照", 18792, 8792, "/", "/api/trials"),
)


class AccessError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def trusted(path, executable=False):
    mode = path.lstat()
    if (not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o022
            or (executable and not os.access(path, os.X_OK))):
        raise ValueError("unexpected file owner, mode or type")
    return path


def registered(base=SUPPORT):
    directory = base / "ssh-tunnel-18791"
    try:
        plist = plistlib.loads(trusted(directory / "tunnel.plist").read_bytes())
        args = plist["ProgramArguments"]
        if plist["Label"] != "com.edgepulse.ssh-tunnel.18791" or args[0] != SSH:
            raise ValueError("unexpected registration")
        user, host = args[-1].rsplit("@", 1)
        port = int(args[args.index("-p") + 1])
        opts = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "-o"]
        if (not USER.fullmatch(user) or not HOST.fullmatch(host) or not 1 <= port <= 65535
                or "StrictHostKeyChecking=yes" not in opts
                or args[args.index("-L")+1] != "127.0.0.1:18791:127.0.0.1:8791"):
            raise ValueError("unexpected host, forward or host key policy")
        proxy = trusted(directory / "connect-direct.py", True)
        command, = [value.partition("=")[2] for value in opts if value.startswith("ProxyCommand=")]
        askpass = trusted(directory / "askpass.py", True)
        if (shlex.split(command) != [str(proxy), "%h", "%p"]
                or plist["EnvironmentVariables"]["SSH_ASKPASS"] != str(askpass)):
            raise ValueError("unexpected SSH helper")
        return dict(host=host, user=user, port=port, proxy=proxy, askpass=askpass)
    except (OSError, ValueError, TypeError, IndexError, KeyError, plistlib.InvalidFileException):
        raise AccessError("未找到可用的已登记服务器配置。", 503) from None


class Prompt:
    """Deliver a password once to OpenSSH askpass over a private Unix socket."""
    def __init__(self, password):
        self.password = password.encode("utf-8")
        self.used = threading.Event()
        self.closed = threading.Event()

    def __enter__(self):
        # macOS Unix sockets have a short path limit, including the temporary directory.
        self.directory = tempfile.TemporaryDirectory(prefix="edge-ask-", dir="/tmp")
        self.path = str(Path(self.directory.name) / "prompt")
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(self.path)
        os.chmod(self.path, 0o600)
        self.sock.listen(1)
        self.sock.settimeout(.25)
        self.thread = threading.Thread(target=self.deliver, daemon=True)
        self.thread.start()
        return self

    def deliver(self):
        while not self.closed.is_set():
            try:
                peer, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with peer:
                try:
                    peer.settimeout(2)
                    peer.sendall(self.password)
                    self.used.set()
                except OSError:
                    pass
                return

    def __exit__(self, *_):
        self.closed.set()
        self.sock.close()
        self.thread.join(timeout=3)
        self.password = b""
        self.directory.cleanup()


def askpass(path):
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(3)
        peer.connect(path)
        with peer.makefile("rb") as stream:
            secret = stream.read(1025)
    if not secret or len(secret) > 1024:
        return 1
    sys.stdout.buffer.write(secret + b"\n")
    return 0


def read_json(port, path, timeout=2.5):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:
            return {}
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, http.client.HTTPException):
        return {}
    finally:
        connection.close()


def listeners(port, *, ssh=False):
    args = ["/usr/sbin/lsof", "-nP", "-a", "-u", str(os.getuid()), "-iTCP:"+str(port), "-sTCP:LISTEN", "-Fp"]
    if ssh:
        args += ["-c", "/^ssh$/"]
    try:
        result = subprocess.run(args, capture_output=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {int(v[1:]) for v in result.stdout.splitlines() if v[:1] == b"p" and v[1:].isdigit()}


def is_free(port):
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def probe(tunnel, target):
    key, name, local, remote, path, api = tunnel
    item = dict(key=key, name=name, url=f"http://127.0.0.1:{local}{path}", connected=False)
    owned = False
    if target:
        forward = f"127.0.0.1:{local}:127.0.0.1:{remote}"
        for pid in listeners(local, ssh=True):
            # Inspect only SSH listeners, never another service occupying this port.
            try:
                command = subprocess.check_output(["/bin/ps", "-p", str(pid), "-o", "command="],
                                                  timeout=2, stderr=subprocess.DEVNULL, text=True)
            except (OSError, subprocess.SubprocessError):
                continue
            fields = command.split()
            options = set(zip(fields, fields[1:]))
            if (("-L", forward) in options and ("-p", str(target["port"])) in options
                    and any(s.endswith("@"+target["host"]) for s in fields)):
                owned = True
                break
    ready = None
    if owned:
        data = read_json(local, api)
        if key == "dashboard":
            item["connected"] = isinstance(data.get("runtime_enabled"), bool) and isinstance(data.get("wallet"), dict)
            if item["connected"] and isinstance(data["wallet"].get("ready"), bool):
                ready = data["wallet"]["ready"]
        else:
            item["connected"] = isinstance(data.get("accounts"), list) and "comparisons" in data and "start_at" in data
    return item, ready, owned


def ssh_connect(target, user, password, missing):
    args = [SSH, "-F", "/dev/null", "-T", "-C", "-p", str(target["port"])]
    opts = ["StrictHostKeyChecking=yes", "UpdateHostKeys=no", "ForwardAgent=no", "ForwardX11=no",
            "ControlMaster=no", "ControlPath=none", "ConnectTimeout=8", "ConnectionAttempts=1",
            "NumberOfPasswordPrompts=1", "ExitOnForwardFailure=yes", "ServerAliveInterval=10",
            "ServerAliveCountMax=3", "ProxyCommand="+shlex.quote(str(target["proxy"]))+" %h %p"]
    if password:
        opts += ["PreferredAuthentications=password", "PubkeyAuthentication=no", "KbdInteractiveAuthentication=no"]
    for opt in opts:
        args += ["-o", opt]
    if missing:
        args += ["-f", "-N"]
        for _, _, local, remote, _, _ in missing:
            args += ["-L", f"127.0.0.1:{local}:127.0.0.1:{remote}"]
    args += [user+"@"+target["host"]]
    if not missing:
        args += ["true"]
    env = dict(PATH="/usr/bin:/bin:/usr/sbin:/sbin", HOME=str(Path.home()), LC_ALL="C",
               DISPLAY="edgepulse:0", SSH_ASKPASS_REQUIRE="force", SSH_ASKPASS=str(target["askpass"]))
    if "SSH_AUTH_SOCK" in os.environ and not password:
        env["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
    with Prompt(password) if password else contextlib.nullcontext() as prompt:
        if prompt:
            env.update(SSH_ASKPASS=str(Path(__file__).resolve()), EDGE_ACCESS_ASKPASS_SOCKET=prompt.path)
        try:
            result = subprocess.run(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True, timeout=16, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise AccessError("SSH 连接超时或无法启动。请检查服务器连接。", 502) from None
        if result.returncode or (prompt and not prompt.used.is_set()):
            raise AccessError("SSH 认证或转发失败。请检查用户名、密码以及已登记的主机密钥。", 502)


class Access:
    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()

    def probes(self, target):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(lambda t: probe(t, target), TUNNELS))

    def status(self):
        try:
            target = registered()
        except AccessError:
            target = None
        reports = self.probes(target)
        return dict(service=SERVICE, server={k:target[k] if target else None for k in ("host","port","user")},
                    tunnels=[r[0] for r in reports], wallet={"ready":reports[0][1]},
                    ssh_available=bool(target and os.access(SSH, os.X_OK)), csrf=self.token)

    def connect(self, payload):
        if not isinstance(payload, dict) or set(payload)-{"user", "password"}:
            raise AccessError("连接参数无效。")
        user, password = payload.get("user", ""), payload.get("password", "")
        if not isinstance(user, str) or not isinstance(password, str):
            raise AccessError("用户名和密码格式无效。")
        target = registered()
        user = user.strip() or target["user"]
        try:
            length = len(password.encode("utf-8"))
        except UnicodeError:
            raise AccessError("密码包含不支持的字符。") from None
        if not USER.fullmatch(user) or length > 1024 or any(c in password for c in "\r\n\0"):
            raise AccessError("用户名或密码格式无效。")
        if not password and user != target["user"]:
            raise AccessError("使用其他用户名时需要填写密码。")
        if not self.lock.acquire(blocking=False):
            raise AccessError("已有连接请求正在处理。", 409)
        try:
            missing = []
            for t, (_, _, owned) in zip(TUNNELS, self.probes(target)):
                if not owned:
                    if not is_free(t[2]):
                        raise AccessError(f"本机端口 {t[2]} 已被其他程序占用。", 409)
                    missing.append(t)
            if missing or password:
                ssh_connect(target, user, password, missing)
            data = self.status()
            if not all(t["connected"] for t in data["tunnels"]):
                raise AccessError("SSH 转发已检查，服务器面板尚未响应。请稍后刷新。", 502)
            data["message"] = "SSH 认证成功，两处面板均已就绪。" if password else "两处服务器面板均已就绪。"
            return data
        finally:
            self.lock.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "EdgePulse"
    sys_version = ""

    def log_message(self, *_):
        pass

    def reply(self, code, data, *, html=False):
        body = data if html else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; connect-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def guard(self, write=False):
        host = f"127.0.0.1:{self.server.server_port}"
        origins = self.headers.get_all("Origin", [])
        if (self.client_address[0] != "127.0.0.1" or self.headers.get_all("Host") != [host]
                or (origins != ["http://"+host] if write else origins not in ([], ["http://"+host]))):
            raise AccessError("仅接受来自本机连接助手页面的请求。", 403)
        if write:
            tokens = self.headers.get_all("X-Access-Token", [])
            if len(tokens) != 1 or not tokens[0].isascii() or not hmac.compare_digest(tokens[0], self.server.access.token):
                raise AccessError("页面凭证已过期，请刷新。", 403)

    def do_GET(self):
        try:
            self.guard()
            if self.path == "/":
                self.reply(200, (BASE / "index.html").read_bytes(), html=True)
            elif self.path == "/health":
                self.reply(200, {"service": SERVICE})
            elif self.path == "/api/status":
                self.reply(200, self.server.access.status())
            else:
                self.reply(404, {"error":"页面不存在。"})
        except AccessError as exc:
            self.reply(exc.status, {"error":str(exc)})

    def do_POST(self):
        try:
            self.guard(write=True)
            if self.path != "/api/connect":
                raise AccessError("页面不存在。", 404)
            if self.headers.get_content_type() != "application/json":
                raise AccessError("请使用 JSON。", 415)
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch("[0-9]{1,4}", lengths[0]):
                raise AccessError("无效的请求长度。", 400)
            size = int(lengths[0])
            if size > 4096:
                raise AccessError("请求内容过大。", 413)
            self.connection.settimeout(4)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise AccessError("请求内容不完整。")
            payload = json.loads(raw)
            result = self.server.access.connect(payload)
            self.reply(200, result)
        except AccessError as exc:
            self.reply(exc.status, {"error":str(exc)})
        except (ValueError, OSError, RecursionError):
            self.reply(400, {"error":"请求读取失败。"})


def launch():
    directory = SUPPORT / "server-access"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = directory / "instance.pid"
    with (directory / "start.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        def running():
            try:
                pid = int(state.read_text().strip())
            except (OSError, ValueError):
                return False
            return pid in listeners(PORT) and read_json(PORT, "/health", .8).get("service") == SERVICE
        if not running():
            if not is_free(PORT):
                raise AccessError(f"本机端口 {PORT} 已被其他程序占用。")
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve"],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=True, close_fds=True)
            for _ in range(35):
                if process.poll() is not None:
                    raise AccessError("连接助手启动失败。")
                if read_json(PORT, "/health", .2).get("service") == SERVICE:
                    if process.pid in listeners(PORT):
                        state.write_text(str(process.pid))
                        break
                time.sleep(.12)
            else:
                process.terminate()
                raise AccessError("连接助手启动超时。")
    return subprocess.call(["/usr/bin/open", f"http://127.0.0.1:{PORT}/"])


def main():
    if os.environ.get("EDGE_ACCESS_ASKPASS_SOCKET"):
        return askpass(os.environ["EDGE_ACCESS_ASKPASS_SOCKET"])
    if sys.argv[1:] == ["--serve"]:
        with ThreadingHTTPServer(("127.0.0.1", PORT), Handler) as server:
            server.access = Access()
            directory = SUPPORT / "server-access"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            (directory / "instance.pid").write_text(str(os.getpid()))
            server.serve_forever()
    elif sys.argv[1:] == ["--open"]:
        return launch()
    else:
        raise AccessError("请双击 start.command 启动。")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AccessError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
