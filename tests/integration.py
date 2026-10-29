#!/usr/bin/env python3
"""显式 --run 才运行真实容器；临时测试项目与生产部署分离。"""
import argparse
import base64
import contextlib
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import sys
import tempfile
import threading
import time
from unittest import mock
import urllib.parse
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import kit


def unused_ports():
    values = set()
    while len(values) < 3:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            values.add(listener.getsockname()[1])
    return sorted(values)


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/subscription"):
            # 模拟订阅不提供认证，覆盖层必须保留部署者的认证设置。
            content = kit.dump_yaml({"allow-lan": False, "authentication": [],
                                     "skip-auth-prefixes": ["0.0.0.0/0"], "proxies": [],
                                     "proxy-groups": [], "rules": ["MATCH,DIRECT"]}).encode()
        elif self.path == "/failure":
            self.send_error(503)
            return
        else:
            content = b"mihomo-kit-integration-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def proxy_http(instance, target, user=None, password=None, tunnel=False):
    headers = {}
    if user is not None:
        raw = (user + ":" + password).encode()
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(raw).decode()
    connection = http.client.HTTPConnection("127.0.0.1", instance.settings["proxy_port"], timeout=10)
    if tunnel:
        parsed = urllib.parse.urlsplit(target)
        connection.set_tunnel(parsed.hostname, parsed.port, headers)
        connection.request("GET", "/echo")
    else:
        connection.request("GET", target, headers=headers)
    response = connection.getresponse()
    result = response.status, response.read()
    connection.close()
    return result


def websocket_check(instance):
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    token = urllib.parse.quote(instance.settings["clash_secret"], safe="")
    with socket.create_connection(("127.0.0.1", instance.settings["api_port"]), timeout=10) as stream:
        request = ("GET /traffic?token=%s HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: %s\r\n\r\n") % (token, key)
        stream.sendall(request.encode())
        reader = stream.makefile("rb")
        assert b"101" in reader.readline(), "WebSocket 未升级"
        headers = {}
        while True:
            line = reader.readline()
            if line == b"\r\n":
                break
            name, value = line.decode().split(":", 1)
            headers[name.lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        assert headers.get("sec-websocket-accept") == expected
        first = reader.read(2)
        assert len(first) == 2 and first[0] & 15 == 1
        length = first[1] & 127
        if length == 126:
            length = struct.unpack("!H", reader.read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", reader.read(8))[0]
        assert length < 1024 * 1024
        frame = json.loads(reader.read(length))
        assert "up" in frame and "down" in frame
        reader.close()


def run():
    if os.name != "posix" or os.geteuid() != 0:
        raise kit.KitError("真实容器测试请在 Linux 测试环境使用 sudo python3 tests/integration.py --run。")
    kit.environment_check()
    service = ThreadingHTTPServer(("0.0.0.0", 0), Fixture)
    threading.Thread(target=service.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="mihomo-kit-ci-") as temporary:
        root = Path(temporary) / "deployment"
        root.mkdir(mode=0o700)
        for name in kit.PROGRAM_FILES + ("deploy.sh",):
            kit.private_write(root / name, (ROOT / name).read_bytes())
            if name.endswith(".sh"):
                os.chmod(root / name, 0o700)
        kit.private_write(root / ".gitignore", (ROOT / ".gitignore").read_bytes())
        def git(directory, *arguments):
            return kit.process(["git"] + list(arguments), cwd=directory)
        git(root, "init", "-b", "main")
        git(root, "config", "user.name", "Deployment Test")
        git(root, "config", "user.email", "test@example.invalid")
        git(root, "add", ".")
        git(root, "commit", "-m", "initial test deployment")
        remote = Path(temporary) / "remote.git"
        git(root, "clone", "--bare", str(root), str(remote))
        git(root, "remote", "add", "origin", str(remote))
        git(root, "push", "-u", "origin", "main")
        control, api, proxy = unused_ports()
        s = {"schema": 1, "kit": "mihomo-deploy-kit", "project": "mkit-ci-" + uuid.uuid4().hex[:10],
             "image": kit.IMAGE, "control_port": control, "api_port": api, "proxy_port": proxy,
             "proxy_bind": "127.0.0.1", "proxy_user": "ci-user", "proxy_password": "1",
             "control_token": secrets.token_hex(32), "clash_secret": secrets.token_hex(32), "subscriptions": []}
        instance = kit.Kit(root, s)
        instance.save()
        instance.write_env()
        (root / "data").mkdir(mode=0o700)
        kit.private_write(root / "data/active.yaml", kit.dump_yaml(dict(kit.policy(s), rules=["MATCH,REJECT"])))
        def cli(*arguments):
            return kit.process([str(root / "deploy.sh")] + list(arguments), cwd=str(root), timeout=900)
        try:
            instance.bootstrap()
            instance.doctor()
            print("PASS 初始化、API 和 HTTP/SOCKS5 正误凭据、端口与挂载")
            assert "用法" in cli("help")
            assert "容器" in cli("status")
            cli("stop")
            assert not instance.running()
            cli("start")
            assert instance.running()
            cli("restart")
            cli("doctor")
            cli("logs")
            print("PASS ./deploy.sh 统一入口的 help/status/stop/start/restart/doctor/logs 实际执行")
            host = next(iter(instance.inspect()["NetworkSettings"]["Networks"].values()))["Gateway"]
            fixture_port = service.server_address[1]
            source_url = "http://127.0.0.1:%s/subscription?token=ci-only-hidden" % fixture_port
            with mock.patch.object(kit, "require_tty"), mock.patch.object(kit.getpass, "getpass", return_value=source_url), \
                 mock.patch.object(kit, "ask", return_value="集成测试订阅"), mock.patch.object(kit, "confirm", return_value=True):
                kit.import_subscription(instance)
            instance.doctor()
            print("PASS 真实订阅下载、官方创建/校验/激活接口、覆盖层保留认证")
            target = "http://%s:%s/echo" % (host, fixture_port)
            for tunnel in (False, True):
                status, body = proxy_http(instance, target, s["proxy_user"], s["proxy_password"], tunnel)
                assert status == 200 and body == b"mihomo-kit-integration-ok"
            assert proxy_http(instance, target)[0] == 407
            print("PASS HTTP 转发及 CONNECT 隧道实际传输")
            websocket_check(instance)
            print("PASS WebSocket token 认证、升级及真实流量数据帧")
            connection = http.client.HTTPConnection("127.0.0.1", api, timeout=5)
            connection.request("GET", "/version", headers={"Origin": "https://untrusted.example"})
            response = connection.getresponse()
            assert response.status in (401, 403)
            assert response.getheader("Access-Control-Allow-Origin") not in ("*", "https://untrusted.example")
            response.read()
            connection.close()
            print("PASS 匿名 API 拒绝、无通配 CORS")
            # SOCKS4 不应绕过带密码的认证；不测试需要 UDP 的功能。
            with socket.create_connection(("127.0.0.1", proxy), timeout=5) as stream:
                packet = b"\x04\x01" + struct.pack("!H", fixture_port) + socket.inet_aton(host) + s["proxy_user"].encode() + b"\x00"
                stream.sendall(packet)
                reply = stream.recv(8)
                assert not reply or len(reply) < 2 or reply[1] != 90, "SOCKS4 绕过了密码认证"
            print("PASS SOCKS4 未绕过密码认证")
            with mock.patch.object(kit, "require_tty"), mock.patch.object(kit, "ask", return_value="1"), \
                 mock.patch.object(kit, "confirm", return_value=True):
                kit.import_subscription(instance, refresh=True)
            print("PASS 订阅更新与再次激活")
            active = (root / "data/active.yaml").read_bytes()
            try:
                kit.download_subscription("http://127.0.0.1:%s/failure?token=do-not-log" % fixture_port)
                raise AssertionError("失败订阅未报错")
            except kit.KitError as error:
                assert "do-not-log" not in str(error)
            assert active == (root / "data/active.yaml").read_bytes()
            print("PASS 导入下载失败不影响已有运行配置且不回显 URL")
            marker = root / "data/integration-marker"
            kit.private_write(marker, "original")
            archive, _ = instance.backup()
            kit.private_write(marker, "changed")
            kit.restore_transaction(instance, archive)
            assert marker.read_text() == "original"
            instance.doctor()
            print("PASS 真实冷备份、校验、恢复和容器重新启动")
            for user, password in (("ci-user", "123"), ("中文 user@example", " a:@$'\\ "),
                                   ("invalid-user", "invalid-password"), ("ci-user", "密" * 85)):
                old_user, old_password = instance.settings["proxy_user"], instance.settings["proxy_password"]
                with mock.patch.object(kit, "ask", return_value=user), mock.patch.object(kit, "new_password", return_value=password):
                    kit.change_auth(instance)
                instance.doctor()
                assert proxy_http(instance, target, old_user, old_password)[0] in (403, 407)
                assert proxy_http(instance, target, user, password)[0] == 200
            print("PASS 单字符/短密码、中文和特殊符号、255 字节密码的真实 HTTP/SOCKS5 认证，旧凭据失效")
            publisher = Path(temporary) / "publisher"
            git(root, "clone", "-b", "main", str(remote), str(publisher))
            git(publisher, "config", "user.name", "Deployment Test")
            git(publisher, "config", "user.email", "test@example.invalid")
            new_program = (publisher / "kit.py").read_text(encoding="utf-8").replace(
                'def update(kit, image=None):\n', 'def update(kit, image=None):\n    print("UPDATED-CODE-EXECUTED")\n', 1)
            kit.private_write(publisher / "kit.py", new_program)
            git(publisher, "add", "kit.py")
            git(publisher, "commit", "-m", "test new updater code")
            git(publisher, "push")
            before_update = json.loads((root / "settings.json").read_text())
            assert "UPDATED-CODE-EXECUTED" in cli("update")
            assert json.loads((root / "settings.json").read_text()) == before_update
            assert marker.read_text() == "original"
            assert git(root, "rev-parse", "HEAD") == git(publisher, "rev-parse", "HEAD")
            print("PASS ./deploy.sh update 自动拉取代码、执行新版、备份、镜像验证，凭据与数据保持")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if instance.inspect()["State"].get("Health", {}).get("Status") == "healthy":
                    break
                time.sleep(1)
            else:
                raise AssertionError("Docker 健康检查未 healthy")
            print("PASS Docker 实际健康检查")
            with mock.patch.object(kit, "confirm", return_value=True):
                args = argparse.Namespace(command="uninstall", argument=None, follow=False, image=None)
                kit.dispatch(instance, args)
            assert instance.container() is None and (root / "data/active.yaml").is_file()
            print("PASS 卸载只移除测试容器并保留持久化数据")
        finally:
            # 仅操作本函数生成的临时唯一项目，无 volumes，无生产数据。
            with contextlib.suppress(Exception):
                instance.compose("down", "--timeout", "15")
            service.shutdown()
            service.server_close()
            backup_dir = root.parent / (root.name + "-backups")
            # TemporaryDirectory 的父目录整体属于本测试，随退出自动清理。
            assert backup_dir.is_relative_to(Path(temporary)) if hasattr(backup_dir, "is_relative_to") else str(backup_dir).startswith(temporary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="真实容器集成测试")
    parser.add_argument("--run", action="store_true", help="显式授权创建临时 Docker 测试项目")
    args = parser.parse_args()
    if args.run:
        run()
    else:
        print("SKIP：未执行真实容器测试。使用 --run 在独立 Linux Docker 测试环境运行。")
