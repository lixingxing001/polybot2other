"""No external SSH sessions or real credentials are used in these tests."""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shlex
import socket
import subprocess
import threading
from unittest import mock

import pytest

source = Path(__file__).resolve().parents[1] / "tools/server-access/server.py"
spec = importlib.util.spec_from_file_location("edge_access_test", source)
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)


@pytest.fixture
def registration(tmp_path):
    folder = tmp_path / "ssh-tunnel-18791"
    folder.mkdir(mode=0o700)
    for filename in ("askpass.py", "connect-direct.py"):
        file = folder / filename
        file.write_text("#!/bin/sh\nexit 1\n")
        file.chmod(0o700)
    args = [access.SSH, "-p", "22", "-o", "StrictHostKeyChecking=yes",
            "-o", "ProxyCommand=" + shlex.quote(str(folder/"connect-direct.py")) + " %h %p",
            "-L", "127.0.0.1:18791:127.0.0.1:8791", "reader@registered.example"]
    plist = dict(Label="com.edgepulse.ssh-tunnel.18791", ProgramArguments=args,
                 EnvironmentVariables={"SSH_ASKPASS":str(folder / "askpass.py")})
    path = folder / "tunnel.plist"
    path.write_bytes(plistlib.dumps(plist))
    path.chmod(0o600)
    return tmp_path, path, plist


def test_safe_registration(registration):
    root, _, _ = registration
    target = access.registered(root)
    assert (target["host"], target["port"], target["user"]) == ("registered.example", 22, "reader")


@pytest.mark.parametrize("change", ["proxy", "host", "strict", "filemode"])
def test_reject_invalid_registration(registration, change):
    root, path, plist = registration
    args = plist["ProgramArguments"]
    if change == "proxy":
        args[6] += "; arbitrary-command"
    if change == "host":
        args[-1] = "reader@-oProxyCommand=echo"
    if change == "strict":
        args[4] = "StrictHostKeyChecking=no"
    if change == "filemode":
        (root / "ssh-tunnel-18791/askpass.py").chmod(0o777)
    path.write_bytes(plistlib.dumps(plist))
    with pytest.raises(access.AccessError):
        access.registered(root)


@pytest.mark.parametrize("missing", [[], list(access.TUNNELS)])
def test_password_ssh_handshake_without_secret_in_arguments_or_environment(registration, tmp_path, monkeypatch, missing):
    root, _, _ = registration
    target = access.registered(root)
    simulated = tmp_path / "ssh"
    sample = "dummy test password! 字"
    # Exercise the real askpass executable through its Unix socket. The fake SSH
    # program authorizes the synthetic credential, while recording only verdicts.
    simulated.write_text("""#!/usr/bin/env python3
import os,sys,subprocess,stat
from pathlib import Path
args=sys.argv[1:]
pwd='dummy test password! 字'
assert not any(pwd in a for a in args)
assert not any(pwd in v for v in os.environ.values())
p=Path(os.environ['EDGE_ACCESS_ASKPASS_SOCKET'])
assert p.stat().st_mode & 0o777 == 0o600 and p.parent.stat().st_mode & 0o777 == 0o700
assert 'StrictHostKeyChecking=yes' in args and 'ForwardAgent=no' in args
assert 'PreferredAuthentications=password' in args and 'PubkeyAuthentication=no' in args
assert len([s for s in args if s.startswith('ProxyCommand=') and s.endswith(' %h %p')]) == 1
assert 'alice@registered.example' in args
if '-N' in args:
 assert '-f' in args and len([i for i in args if i=='-L'])==2
 assert '127.0.0.1:18792:127.0.0.1:8792' in args
else:
 assert args[-1]=='true' and '-L' not in args
result=subprocess.check_output([os.environ['SSH_ASKPASS'], 'password:']).decode().strip()
Path(__file__).with_name('prompt-location').write_text(str(p))
sys.exit(0 if result==pwd else 1)
""")
    simulated.chmod(0o700)
    monkeypatch.setattr(access, "SSH", str(simulated))
    access.ssh_connect(target, "alice", sample, missing)
    sock = Path((tmp_path / "prompt-location").read_text())
    assert not sock.parent.exists()
    with pytest.raises(access.AccessError, match="认证"):
        access.ssh_connect(target, "alice", "wrong", missing)
    assert not Path((tmp_path / "prompt-location").read_text()).parent.exists()


def test_require_password_prompt_and_timeout(registration, monkeypatch):
    root, _, _ = registration
    target = access.registered(root)
    run = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(subprocess,"run",run)
    with pytest.raises(access.AccessError,match="认证"):
        access.ssh_connect(target, "reader", "dummy", [])
    run.side_effect=subprocess.TimeoutExpired([], 16)
    with pytest.raises(access.AccessError,match="超时"):
        access.ssh_connect(target, "reader", "", [])


def reports():
    return [(dict(key=key, connected=True),False,True) for key, *_ in access.TUNNELS]


def test_manual_login_is_tested_even_when_original_forwards_healthy(registration, monkeypatch):
    root, _, _ = registration
    target=access.registered(root)
    monkeypatch.setattr(access,"registered",lambda:target)
    owner=access.Access()
    monkeypatch.setattr(owner,"probes",lambda _:reports())
    run=mock.Mock()
    monkeypatch.setattr(access,"ssh_connect",run)
    assert owner.connect({})["ssh_available"]
    run.assert_not_called()
    owner.connect(dict(user="alice",password="supplied"))
    run.assert_called_once_with(target,"alice","supplied",[])
    run.side_effect=access.AccessError("bad",502)
    with pytest.raises(access.AccessError):
        owner.connect(dict(user="alice",password="invalid"))
    assert not owner.lock.locked()


def test_blocks_conflicts_and_unapproved_hosts(registration, monkeypatch):
    root, _, _ = registration
    target=access.registered(root)
    monkeypatch.setattr(access,"registered",lambda:target)
    owner=access.Access()
    monkeypatch.setattr(owner,"probes",lambda _: [(a,b,False) for a,b,_ in reports()])
    run=mock.Mock()
    monkeypatch.setattr(access,"ssh_connect",run)
    monkeypatch.setattr(access,"is_free",lambda _port:False)
    for fields in ({"host":"evil"}, {"user":"other"}, {"password":"A\nB"}, {"user":"-Fbad"}, {"user":None}):
        with pytest.raises(access.AccessError):
            owner.connect(fields)
    with pytest.raises(access.AccessError,match="其他程序占用"):
        owner.connect({})
    with owner.lock, pytest.raises(access.AccessError,match="正在处理"):
        owner.connect({})
    run.assert_not_called()


def test_fresh_status_wallet_unknown_when_dashboard_data_unavailable(monkeypatch):
    monkeypatch.setattr(access, "registered", mock.Mock(side_effect=access.AccessError("missing")))
    status=access.Access().status()
    assert not status["ssh_available"] and status["wallet"]=={"ready":None}
    assert all(not r["connected"] for r in status["tunnels"])


def test_http_rejects_foreign_origins_hosts_tokens_and_oversize():
    with access.ThreadingHTTPServer(("127.0.0.1",0),access.Handler) as server:
        fake=mock.Mock(token="test-token")
        fake.status.return_value=dict(csrf="test-token")
        fake.connect.return_value={"message":"ok"}
        server.access=fake
        task=threading.Thread(target=server.serve_forever,daemon=True)
        task.start()
        try:
            port=server.server_port
            host=f"127.0.0.1:{port}"
            safe={"Content-Type":"application/json","Origin":"http://"+host,"X-Access-Token":"test-token"}
            def call(method,path,data=None,headers=None):
                cn=http.client.HTTPConnection("127.0.0.1",port,timeout=3)
                try:
                    cn.request(method,path,body=data,headers=headers or {})
                    r=cn.getresponse()
                    return r.status,json.loads(r.read())
                finally:
                    cn.close()
            assert call("GET","/api/status")== (200,{"csrf":"test-token"})
            assert call("GET","/api/status",headers={"Host":"rebinding.invalid"})[0]==403
            assert call("GET","/api/status",headers={"Origin":"null"})[0]==403
            assert call("POST","/api/connect","{}",{**safe,"Origin":"http://example.com"})[0]==403
            assert call("POST","/api/connect","{}",{**safe,"X-Access-Token":"wrong"})[0]==403
            assert call("POST","/api/connect","{}",{"Content-Type":"application/json","X-Access-Token":"test-token"})[0]==403
            assert call("POST","/api/connect","x"*4097,safe)[0]==413
            assert call("POST","/api/connect","{",safe)[0]==400
            assert call("POST","/api/connect","{}",{**safe,"Content-Type":"text/plain"})[0]==415
            assert call("POST","/api/connect","{}",safe)==(200,{"message":"ok"})
            fake.connect.assert_called_once_with({})
        finally:
            server.shutdown()
            task.join(timeout=3)
