#!/usr/bin/env python3
"""轻量部署及管理入口。敏感信息只通过文件、内存或标准输入传递。"""
import argparse
import base64
import contextlib
import datetime as dt
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:
    import yaml
except ImportError:
    yaml = None

IMAGE = "ghcr.io/metacubex/metacubexd-server:v1.273.1"
SCHEMA = 1
SOURCE = Path(__file__).resolve().parent
PROGRAM_FILES = ("compose.yaml", "install.sh", "manage.sh", "kit.py", "README.md")
BACKUP_ITEMS = PROGRAM_FILES + (".env", "settings.json", "data")
MAX_DOWNLOAD = 8 * 1024 * 1024


class KitError(Exception):
    pass


def need_yaml():
    if yaml is None:
        raise KitError("缺少 PyYAML。Ubuntu 请运行：sudo apt-get install python3-yaml")


def timestamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def require_tty():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise KitError("交互操作需要终端，请直接运行脚本，不要通过管道传入敏感信息。")


def confirm(message, default=False):
    require_tty()
    suffix = " [Y/n] " if default else " [y/N] "
    answer = input(message + suffix).strip().lower()
    return default if not answer else answer in ("y", "yes", "是")


def ask(message, default=None):
    require_tty()
    answer = input(message + (" [%s]" % default if default is not None else "") + "：").strip()
    return answer or default


def new_password():
    require_tty()
    first = getpass.getpass("代理密码（隐藏输入，留空生成随机密码）：")
    if not first:
        return secrets.token_urlsafe(24)
    if first != getpass.getpass("再次输入代理密码："):
        raise KitError("两次输入的密码不同。")
    validate_credentials("check", first)
    return first


def validate_credentials(user, password):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", user):
        raise KitError("用户名限 1–64 位英文字母、数字、下划线、点和短横线。")
    if not 16 <= len(password.encode("utf-8")) <= 128 or any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise KitError("密码须为 16–128 字节且不能含控制字符。")


def port_number(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise KitError("端口必须是数字。")
    if not 1024 <= number <= 65535:
        raise KitError("端口范围为 1024–65535。")
    return number


def safe_yaml(text):
    need_yaml()
    try:
        value = yaml.safe_load(text)
    except (yaml.YAMLError, UnicodeError):
        raise KitError("配置不是有效 YAML；未输出可能包含凭据的原始解析错误。")
    if not isinstance(value, dict):
        raise KitError("配置顶层必须是 YAML 对象，需要 Mihomo/Clash YAML 订阅。")
    return value


def dump_yaml(value):
    need_yaml()
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False, default_flow_style=False)


def private_write(path, data):
    path = Path(path)
    if path.is_symlink():
        raise KitError("拒绝覆盖符号链接：" + path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def json_write(path, value):
    private_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def digest_file(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def process(args, cwd=None, timeout=120):
    try:
        result = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, text=True, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        raise KitError("命令启动失败或超时：" + args[0])
    if result.returncode:
        # Docker / 上游错误可能包含环境变量或订阅内容，不直接回显。
        raise KitError("命令失败：%s（退出码 %s）。可运行 manage.sh doctor 或 logs 检查。" % (args[0], result.returncode))
    return result.stdout


def free_port(bind, port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((bind, port))
    except OSError:
        raise KitError("端口已占用或不可绑定：%s:%s" % (bind, port))


def validate_image(image):
    if not re.fullmatch(r"ghcr\.io/metacubex/metacubexd-server:v[0-9]+\.[0-9]+\.[0-9]+(?:@sha256:[a-f0-9]{64})?", image):
        raise KitError("只接受官方 metacubexd-server 的明确版本标签，可附加 SHA256 摘要。")


def policy(settings):
    return {
        "allow-lan": True, "bind-address": "*",
        "authentication": [settings["proxy_user"] + ":" + settings["proxy_password"]],
        "skip-auth-prefixes": [], "port": 0, "socks-port": 0,
        "redir-port": 0, "tproxy-port": 0, "listeners": [],
        "tun": {"enable": False}, "log-level": "warning",
        "external-controller-cors": {
            "allow-origins": ["http://127.0.0.1:%s" % settings["control_port"],
                              "http://localhost:%s" % settings["control_port"]],
            "allow-private-network": False,
        },
    }


def check_policy(config, settings):
    expected = policy(settings)
    for key in ("allow-lan", "bind-address", "authentication", "skip-auth-prefixes", "listeners"):
        if config.get(key) != expected[key]:
            raise KitError("代理安全设置被覆盖：" + key)
    if config.get("tun", {}).get("enable", False):
        raise KitError("精简套件不启用 TUN。")
    cors = config.get("external-controller-cors", {}) or {}
    origins = cors.get("allow-origins", [])
    if not isinstance(origins, list) or any(not isinstance(v, str) or "*" in v for v in origins):
        raise KitError("CORS 只能使用明确 Origin，不能使用通配符。")


@contextlib.contextmanager
def deployment_lock(root):
    import fcntl
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(Path(root) / ".install.lock"), flags, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise KitError("已有安装或管理操作正在执行，请稍后重试。")
        yield
    finally:
        os.close(descriptor)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise KitError("本地管理接口发生意外重定向，已停止发送认证信息。")


class Kit:
    def __init__(self, root, settings=None):
        self.root = Path(root).resolve()
        self.settings = settings if settings is not None else json.loads((self.root / "settings.json").read_text("utf-8"))
        self.validate_settings()
        self.api_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def validate_settings(self):
        s = self.settings
        if s.get("schema") != SCHEMA or s.get("kit") != "mihomo-deploy-kit":
            raise KitError("不兼容或不属于本套件的部署配置，拒绝覆盖。")
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,45}", s.get("project", "")):
            raise KitError("Compose 项目名称无效。")
        for field in ("control_token", "clash_secret"):
            if not re.fullmatch(r"[a-f0-9]{64}", s.get(field, "")):
                raise KitError("管理密钥格式无效；不会自动覆盖现有密钥。")
        ports = [port_number(s[k]) for k in ("control_port", "api_port", "proxy_port")]
        if len(set(ports)) != 3:
            raise KitError("三个宿主机端口必须不同。")
        if s.get("proxy_bind") not in ("127.0.0.1", "0.0.0.0"):
            raise KitError("代理绑定地址无效。")
        validate_credentials(s["proxy_user"], s["proxy_password"])
        validate_image(s["image"])

    @property
    def prefix(self):
        return ["docker", "compose", "--project-name", self.settings["project"],
                "--project-directory", str(self.root), "--env-file", str(self.root / ".env"),
                "-f", str(self.root / "compose.yaml")]

    def compose(self, *args, timeout=180):
        return process(self.prefix + list(args), cwd=str(self.root), timeout=timeout)

    def save(self):
        self.validate_settings()
        json_write(self.root / "settings.json", self.settings)

    def write_env(self, bind=None):
        s = self.settings
        values = {"IMAGE": s["image"], "CONTROL_HOST_PORT": s["control_port"],
                  "API_HOST_PORT": s["api_port"], "PROXY_HOST_PORT": s["proxy_port"],
                  "PROXY_BIND": bind or s["proxy_bind"], "CONTROL_TOKEN": s["control_token"],
                  "CLASH_SECRET": s["clash_secret"], "DEFAULT_BACKEND_URL": s.get("backend_url", "")}
        if any("\n" in str(v) or "\r" in str(v) for v in values.values()):
            raise KitError("环境变量含换行，拒绝生成。")
        # 所有变量均由套件生成或严格校验；不把代理密码或订阅 URL 放入 .env。
        private_write(self.root / ".env", "".join("%s='%s'\n" % (k, str(v).replace("'", "\\'")) for k, v in values.items()))

    def request(self, path, method="GET", body=None, core=False, authenticated=True, raw=False, timeout=320):
        s = self.settings
        port = s["api_port"] if core else s["control_port"]
        endpoint = "http://127.0.0.1:%s%s" % (port, path if core else "/api/control" + path)
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + s["clash_secret" if core else "control_token"]
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(endpoint, data=data, headers=headers, method=method)
        try:
            with self.api_opener.open(request, timeout=timeout) as response:
                content = response.read(MAX_DOWNLOAD + 1)
            if len(content) > MAX_DOWNLOAD:
                raise KitError("管理接口响应过大。")
            return content.decode("utf-8") if raw else (json.loads(content) if content else None)
        except urllib.error.HTTPError as error:
            error.close()
            raise KitError("管理接口 %s 返回 HTTP %s；未输出敏感响应内容。" % (path.split("?")[0], error.code))
        except (urllib.error.URLError, TimeoutError, ValueError, UnicodeError, OSError):
            raise KitError("管理接口暂不可用或响应格式无效。")

    def container(self):
        ids = self.compose("ps", "-a", "-q", "server").split()
        if len(ids) > 1:
            raise KitError("发现多个核心容器，请人工检查。")
        return ids[0] if ids else None

    def inspect(self):
        container = self.container()
        if not container:
            return None
        data = json.loads(process(["docker", "inspect", container]))[0]
        labels = data["Config"].get("Labels", {})
        mounts = [m for m in data.get("Mounts", []) if m.get("Destination") == "/data"]
        if labels.get("com.docker.compose.project") != self.settings["project"] or labels.get("com.docker.compose.service") != "server":
            raise KitError("容器归属不一致，拒绝操作。")
        if len(mounts) != 1 or mounts[0].get("Type") != "bind" or Path(mounts[0]["Source"]).resolve() != self.root / "data":
            raise KitError("容器实际数据挂载与本套件不一致，拒绝操作。")
        return data

    def running(self):
        data = self.inspect()
        return bool(data and data["State"]["Running"])

    def stop(self):
        self.inspect()
        self.compose("stop", "-t", "25", "server")

    def ready(self, timeout=100):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                health = self.request("/health", timeout=3)
                state = self.request("/kernel/status", timeout=3)
                version = self.request("/version", core=True, timeout=3)
                if health.get("ok") is True and state.get("status") == "running" and version.get("version"):
                    return version["version"]
            except KitError:
                pass
            time.sleep(1)
        raise KitError("等待面板或内核就绪超时；数据已保留，请运行 doctor 和 logs。")

    def runtime_check(self):
        config = safe_yaml(self.request("/config/runtime", raw=True))
        check_policy(config, self.settings)
        for path, core in (("/version", True), ("/kernel/status", False)):
            try:
                self.request(path, core=core, authenticated=False, timeout=5)
            except KitError as error:
                if "HTTP 401" in str(error) or "HTTP 403" in str(error):
                    continue
                raise
            raise KitError("管理接口未拒绝匿名请求。")
        self.proxy_auth_check()

    def up_checked(self, temporary_bind=None):
        check_policy(safe_yaml((self.root / "data/active.yaml").read_text("utf-8")), self.settings)
        self.inspect()
        self.write_env(bind=temporary_bind)
        self.compose("config", "--quiet")
        self.compose("up", "-d", "--no-deps", "--pull", "never", "server", timeout=180)
        try:
            self.ready()
            self.runtime_check()
        except Exception:
            self.stop()
            raise KitError("启动验证失败，已停止本项目容器，避免认证失效。原数据未删除。")

    def proxy_auth_check(self):
        address = ("127.0.0.1", self.settings["proxy_port"])
        def receive(stream, count):
            result = b""
            while len(result) < count:
                chunk = stream.recv(count - len(result))
                if not chunk:
                    raise KitError("代理认证握手意外结束。")
                result += chunk
            return result
        try:
            for credential in (None, "invalid-user:invalid-password"):
                with socket.create_connection(address, timeout=5) as stream:
                    header = b""
                    if credential:
                        header = b"Proxy-Authorization: Basic " + base64.b64encode(credential.encode()) + b"\r\n"
                    stream.sendall(b"GET http://example.invalid/ HTTP/1.1\r\nHost: example.invalid\r\n" + header + b"Connection: close\r\n\r\n")
                    status = stream.recv(512).split(b"\r\n", 1)[0].split()
                    expected = (b"407",) if credential is None else (b"403", b"407")
                    if len(status) < 2 or status[1] not in expected:
                        raise KitError("HTTP 代理未正确拒绝无凭据或错误凭据。")
            with socket.create_connection(address, timeout=5) as stream:
                stream.sendall(b"\x05\x01\x00")
                # 上游在启用认证时返回 0x02，要求账号密码，即使客户端只提议匿名；
                # 这同样拒绝匿名，不应错误地要求它必须返回 0xff。
                if receive(stream, 2) not in (b"\x05\xff", b"\x05\x02"):
                    raise KitError("SOCKS5 接受了匿名认证。")
            for user, password, success in (("invalid-user", "invalid-password", False),
                                           (self.settings["proxy_user"], self.settings["proxy_password"], True)):
                with socket.create_connection(address, timeout=5) as stream:
                    stream.sendall(b"\x05\x01\x02")
                    if receive(stream, 2) != b"\x05\x02":
                        raise KitError("SOCKS5 未启用账号密码认证。")
                    ub, pb = user.encode("utf-8"), password.encode("utf-8")
                    stream.sendall(b"\x01" + bytes([len(ub)]) + ub + bytes([len(pb)]) + pb)
                    reply = receive(stream, 2)
                    if reply[0] != 1 or (reply[1] == 0) != success:
                        raise KitError("SOCKS5 账号密码校验结果不正确。")
        except (OSError, TimeoutError):
            raise KitError("代理认证连接失败或超时。")

    def ensure_overlay(self, create=False):
        entries = self.request("/profiles")
        if any(p.get("type") == "script" and p.get("enabled") is not False for p in entries):
            raise KitError("检测到启用的脚本配置，请先人工审查；套件不会执行未知配置脚本。")
        profile_id = self.settings.get("policy_id")
        existing = next((p for p in entries if p["id"] == profile_id), None)
        if existing:
            value = self.request("/profiles/" + profile_id)
            if existing.get("type") != "merge" or existing.get("enabled") is False:
                raise KitError("本地访问设置覆盖层已被修改或停用，请恢复后重试。")
            check_policy(safe_yaml(value["content"]), self.settings)
        else:
            if profile_id or not create:
                raise KitError("本地访问设置覆盖层已被删除，拒绝继续激活订阅。")
            result = self.request("/profiles", "POST", {
                "name": "本地访问设置（请勿停用）", "type": "merge", "content": dump_yaml(policy(self.settings))})
            self.settings["policy_id"] = result["id"]
            self.save()

    def bootstrap(self):
        # 可重试：沿用已有密钥、代理账号及候选文件，不清空现有数据。
        self.compose("config", "--quiet")
        self.compose("pull", "server", timeout=900)
        self.up_checked(temporary_bind="127.0.0.1")
        self.ensure_overlay(create=True)
        entries = self.request("/profiles")
        active = next((p for p in entries if p.get("active") and p.get("type") not in ("merge", "script")), None)
        if active is None:
            baseline = dict(policy(self.settings), **{"mode": "rule", "proxies": [], "proxy-groups": [], "rules": ["MATCH,REJECT"]})
            active = self.request("/profiles", "POST", {
                "name": "初始配置（拒绝出站，等待导入）", "type": "local", "content": dump_yaml(baseline)})
        self.activate(active["id"])
        self.up_checked()
        self.settings["initialized"] = True
        self.save()

    def activate(self, profile_id):
        if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", profile_id):
            raise KitError("配置 ID 无效。")
        self.ensure_overlay()
        preview = self.request("/profiles/" + profile_id + "/editor")
        check_policy(safe_yaml(preview["composedYaml"]), self.settings)
        previous = next((p["id"] for p in self.request("/profiles") if p.get("active")), None)
        try:
            state = self.request("/profiles/" + profile_id + "/activate", "POST", {})
            if state.get("status") != "running":
                raise KitError("内核未成功加载候选配置。")
            self.ready()
            self.runtime_check()
            active = self.request("/profiles")
            if not any(p["id"] == profile_id and p.get("active") for p in active):
                raise KitError("激活状态与预期不符。")
        except Exception:
            try:
                if previous:
                    self.request("/profiles/" + previous + "/activate", "POST", {})
                    self.ready()
                    self.runtime_check()
                else:
                    raise KitError("没有先前激活配置。")
            except Exception:
                self.stop()
                raise KitError("配置激活或恢复验证失败，已停止本项目容器；配置和备份均保留。")
            raise KitError("候选配置激活失败，已验证恢复此前配置；导入文件保留供检查。")

    def status(self):
        data = self.inspect()
        if not data:
            print("本项目容器不存在；本地配置和数据仍可保留。")
            return
        state = data["State"]
        print("容器：%s  状态：%s  健康：%s" % (data["Name"].lstrip("/"), state["Status"], state.get("Health", {}).get("Status", "未提供")))
        print("实际端口：" + json.dumps(data.get("NetworkSettings", {}).get("Ports", {}), ensure_ascii=False))
        if state.get("Running"):
            try:
                kernel = self.request("/kernel/status", timeout=5)
                print("内核：%s  版本：%s" % (kernel.get("status"), kernel.get("version", "未知")))
            except KitError as error:
                print(str(error))
        print("安装目录：%s\n持久化目录：%s" % (self.root, self.root / "data"))

    def doctor(self):
        self.compose("config", "--quiet")
        data = self.inspect()
        if not data or not data["State"]["Running"]:
            raise KitError("容器没有运行；数据未删除。")
        self.ready(timeout=10)
        self.ensure_overlay()
        self.runtime_check()
        expected_bind = self.settings["proxy_bind"]
        ports = data.get("NetworkSettings", {}).get("Ports", {})
        for internal, field, bind in ((9090, "control_port", "127.0.0.1"),
                                      (9091, "api_port", "127.0.0.1"),
                                      (9092, "proxy_port", expected_bind)):
            mapped = ports.get("%s/tcp" % internal) or []
            if len(mapped) != 1 or mapped[0]["HostIp"] != bind or int(mapped[0]["HostPort"]) != self.settings[field]:
                raise KitError("实际端口映射与预期不同：容器端口 %s" % internal)
        if os.name == "posix":
            for path in (self.root, self.root / "data", self.root / ".env", self.root / "settings.json"):
                if stat.S_IMODE(path.stat().st_mode) & 0o077:
                    raise KitError("敏感文件或目录权限过宽：" + path.name)
        print("诊断通过：Compose、数据挂载、实际端口、内核、API 认证、HTTP/SOCKS5 认证及本地访问设置均正常。")
        print("网络：" + ", ".join(data.get("NetworkSettings", {}).get("Networks", {}).keys()))
        print("出站连通性、云安全组、公网访问及用户自行配置的反向代理需另外验证。")

    def scrub(self, text):
        for value in (self.settings["control_token"], self.settings["clash_secret"], self.settings["proxy_password"]):
            if value:
                text = text.replace(value, "[已隐藏]")
        text = re.sub(r"(?i)(?:https?|socks5h?)://[^\s\"'<>]+", "[URL已隐藏]", text)
        return text

    def logs(self, follow=False):
        self.inspect()
        args = self.prefix + ["logs", "--no-color", "--tail", "100"]
        if follow:
            args.append("--follow")
        args.append("server")
        child = subprocess.Popen(args, cwd=str(self.root), stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        try:
            for line in child.stdout:
                print(self.scrub(line.rstrip()), flush=True)
            if child.wait():
                raise KitError("读取日志失败。")
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)

    def backup(self, resume=True):
        directory = self.root.parent / (self.root.name + "-backups")
        if directory.is_symlink():
            raise KitError("备份目录不能是符号链接。")
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        was_running = self.running()
        print("备份：短暂停止本项目容器以获得一致快照，其他项目不受影响。")
        if was_running:
            self.stop()
        path = directory / (timestamp() + ".tar.gz")
        try:
            create_archive(self.root, path)
            verify_archive(path)
            private_write(str(path) + ".sha256", digest_file(path) + "  " + path.name + "\n")
        finally:
            if was_running and resume:
                self.up_checked()
        print("备份已验证：" + str(path))
        return path, was_running

    def report(self):
        s = self.settings
        self.status()
        print("面板：http://127.0.0.1:%s（仅服务器本机，可自行配置反向代理或 SSH 隧道）" % s["control_port"])
        print("API：http://127.0.0.1:%s（使用 CLASH_SECRET）" % s["api_port"])
        print("代理：%s:%s/tcp；用户名：%s；密码已隐藏。" % (s["proxy_bind"], s["proxy_port"], s["proxy_user"]))
        print("查看凭据：bash %s/manage.sh show-secret proxy" % self.root)
        print("管理命令：bash %s/manage.sh help" % self.root)
        print("本项目不配置域名、HTTPS、证书、反向代理或系统防火墙。")


def create_archive(root, destination):
    root, destination = Path(root), Path(destination)
    if destination.exists():
        raise KitError("备份目标已存在，拒绝覆盖。")
    members, hashes = [], {}
    for name in BACKUP_ITEMS:
        path = root / name
        if not path.exists():
            raise KitError("备份缺少必要文件：" + name)
        candidates = [path] + (sorted(path.rglob("*")) if path.is_dir() else [])
        for candidate in candidates:
            if candidate.is_symlink() or not (candidate.is_dir() or candidate.is_file()):
                raise KitError("备份遇到链接或特殊文件，已停止；不跟随链接。")
            members.append(candidate)
            if candidate.is_file():
                hashes[candidate.relative_to(root).as_posix()] = digest_file(candidate)
    descriptor = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream, tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for path in members:
            archive.add(str(path), arcname=path.relative_to(root).as_posix(), recursive=False)
        manifest = json.dumps({"schema": SCHEMA, "files": hashes}, sort_keys=True).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size, info.mode = len(manifest), 0o600
        archive.addfile(info, io.BytesIO(manifest))


def verify_archive(path, extract_to=None):
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names = set()
        for member in members:
            name = member.name
            parts = name.split("/")
            if name.startswith("/") or "\\" in name or any(p in ("", ".", "..") for p in parts) or name in names:
                raise KitError("备份含非法或重复路径。")
            if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise KitError("备份含链接或特殊文件。")
            if parts[0] not in BACKUP_ITEMS + ("manifest.json",):
                raise KitError("备份含未知顶层文件。")
            names.add(name)
        manifest_member = archive.getmember("manifest.json")
        if manifest_member.size > 8 * 1024 * 1024:
            raise KitError("备份清单过大。")
        manifest = json.load(archive.extractfile(manifest_member))
        if manifest.get("schema") != SCHEMA:
            raise KitError("备份版本不兼容。")
        expected = manifest["files"]
        actual_files = {m.name for m in members if m.isfile() and m.name != "manifest.json"}
        if set(expected) != actual_files or not set(BACKUP_ITEMS).issubset(names):
            raise KitError("备份文件清单不完整。")
        for name, expected_digest in expected.items():
            digest = hashlib.sha256()
            with archive.extractfile(name) as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected_digest:
                raise KitError("备份完整性校验失败。")
        if extract_to is not None:
            extract_to = Path(extract_to)
            for member in members:
                if member.name == "manifest.json":
                    continue
                target = extract_to / member.name
                if target.is_symlink():
                    raise KitError("恢复目标含符号链接。")
                if member.isdir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    os.chmod(target, 0o700)
                else:
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with archive.extractfile(member) as source:
                        private_write(target, source.read())
    return True


def download_subscription(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.fragment:
            raise KitError("订阅地址须为有效 HTTP/HTTPS URL，不能包含片段。")
        headers = {"User-Agent": "clash.meta"}
        handlers = []
        if parsed.username is not None:
            host = parsed.hostname
            if ":" in host:
                host = "[" + host + "]"
            if parsed.port:
                host += ":" + str(parsed.port)
            clean = urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, parsed.query, ""))
            manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            manager.add_password(None, clean, urllib.parse.unquote(parsed.username), urllib.parse.unquote(parsed.password or ""))
            handlers.append(urllib.request.HTTPBasicAuthHandler(manager))
        else:
            clean = url
        opener = urllib.request.build_opener(*handlers)
        with opener.open(urllib.request.Request(clean, headers=headers), timeout=45) as response:
            if urllib.parse.urlsplit(response.geturl()).scheme not in ("http", "https"):
                raise KitError("订阅重定向协议不受支持。")
            content = response.read(MAX_DOWNLOAD + 1)
        if len(content) > MAX_DOWNLOAD:
            raise KitError("订阅超过 8 MiB，已停止。")
        text = content.decode("utf-8-sig")
        safe_yaml(text)
        return text
    except KitError:
        raise
    except Exception:
        # 上游异常可能直接包含 URL，不能输出异常文本。
        raise KitError("订阅下载失败，请检查网络、证书、链接或供应商状态；原配置未删除。")


def import_subscription(kit, refresh=False):
    kit.runtime_check()
    kit.ensure_overlay()
    sources = kit.settings.setdefault("subscriptions", [])
    previous_source = None
    if refresh:
        if not sources:
            raise KitError("没有由本套件导入的订阅，请先运行 import-subscription。")
        for index, source in enumerate(sources, 1):
            print("%s. %s" % (index, source["name"]))
        try:
            number = int(ask("选择要更新的订阅序号", "1"))
            if number < 1:
                raise ValueError()
            previous_source = sources[number - 1]
        except (ValueError, IndexError):
            raise KitError("订阅序号无效。")
        url, name = previous_source["url"], previous_source["name"]
    else:
        require_tty()
        url = getpass.getpass("订阅 URL（隐藏输入）：").strip()
        name = ask("配置名称", "主订阅")
        if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise KitError("配置名称须为 1–80 个可打印字符。")
    active = confirm("下载并验证后立即激活？", True)
    content = download_subscription(url)
    kit.backup()
    created = kit.request("/profiles", "POST", {"name": name, "type": "local", "content": content})
    profile_id = created["id"]
    preview = kit.request("/profiles/" + profile_id + "/editor")
    check_policy(safe_yaml(preview["composedYaml"]), kit.settings)
    # 官方 validate 校验源文件；activate 还会验证合成后的实际配置。
    validation = kit.request("/profiles/" + profile_id + "/validate", "POST", {})
    if validation.get("valid") is not True:
        raise KitError("候选订阅未通过内核校验；原配置继续运行，候选配置保留供检查。")
    if active:
        kit.activate(profile_id)
    record = {"id": profile_id, "name": name, "url": url, "updated_at": timestamp()}
    if previous_source:
        sources[sources.index(previous_source)] = record
    else:
        sources.append(record)
    kit.save()
    print("订阅已导入并%s；配置 ID：%s。链接仅保存在受限 settings.json 中。" % ("激活" if active else "保存，尚未激活", profile_id))


def choose_profile(kit):
    entries = [p for p in kit.request("/profiles") if p.get("type") not in ("merge", "script")]
    if not entries:
        raise KitError("没有可激活的配置。")
    for index, entry in enumerate(entries, 1):
        print("%s. %s%s" % (index, entry["name"], "（当前）" if entry.get("active") else ""))
    try:
        number = int(ask("选择配置序号"))
        if number < 1:
            raise ValueError()
        profile_id = entries[number - 1]["id"]
    except (ValueError, TypeError, IndexError):
        raise KitError("配置序号无效。")
    kit.backup()
    kit.activate(profile_id)
    print("配置已激活，内核及代理认证验证通过。")


def restore_transaction(kit, path, start=True):
    with tempfile.TemporaryDirectory(prefix=".restore-stage-", dir=str(kit.root.parent)) as temporary:
        staging = Path(temporary)
        os.chmod(staging, 0o700)
        verify_archive(path, staging)
        candidate = Kit(staging)
        if candidate.settings["project"] != kit.settings["project"]:
            raise KitError("备份属于另一个 Compose 项目，拒绝交叉恢复。")
        check_policy(safe_yaml((staging / "data/active.yaml").read_text("utf-8")), candidate.settings)
        candidate.compose("config", "--quiet")
        quarantine = kit.root / (".restore-" + timestamp())
        quarantine.mkdir(mode=0o700)
        kit.stop()
        moved = []
        try:
            for name in BACKUP_ITEMS:
                current = kit.root / name
                if current.exists():
                    os.replace(current, quarantine / name)
                moved.append(name)
                os.replace(staging / name, current)
            restored = Kit(kit.root)
            if start:
                restored.up_checked()
            kit.settings = restored.settings
        except Exception:
            failed = kit.root / (".failed-restore-" + timestamp())
            failed.mkdir(mode=0o700)
            with contextlib.suppress(Exception):
                Kit(kit.root).stop()
            for name in reversed(moved):
                current, old = kit.root / name, quarantine / name
                if current.exists():
                    os.replace(current, failed / name)
                if old.exists():
                    os.replace(old, current)
            previous = Kit(kit.root)
            if start:
                previous.up_checked()
            kit.settings = previous.settings
            raise KitError("恢复失败，已还原恢复前状态；失败文件隔离保留，未删除数据。")
        print("恢复完成。恢复前文件另外保留在：" + str(quarantine))


def restore_interactive(kit, path=None):
    require_tty()
    path = Path(path or ask("备份 .tar.gz 的完整路径")).expanduser().resolve()
    verify_archive(path)
    print("将恢复：" + str(path))
    if not confirm("这会替换本项目配置和数据，确认继续？"):
        print("已取消，未修改数据。")
        return
    backup_path, running = kit.backup()
    print("当前状态已备份：" + str(backup_path))
    restore_transaction(kit, path, start=running)


def change_auth(kit):
    user = ask("新代理用户名", kit.settings["proxy_user"])
    password = new_password()
    validate_credentials(user, password)
    backup_path, running = kit.backup(resume=False)
    try:
        profile_id = kit.settings.get("policy_id", "")
        if not re.fullmatch(r"[a-zA-Z0-9-]{1,80}", profile_id):
            raise KitError("本地访问设置 ID 无效。")
        profile_path = kit.root / "data/profiles" / (profile_id + ".yaml")
        if not profile_path.is_file():
            raise KitError("本地访问设置不存在。")
        kit.settings.update(proxy_user=user, proxy_password=password)
        private_write(profile_path, dump_yaml(policy(kit.settings)))
        active_path = kit.root / "data/active.yaml"
        config = safe_yaml(active_path.read_text("utf-8"))
        config.update(policy(kit.settings))
        private_write(active_path, dump_yaml(config))
        kit.save()
        if running:
            kit.up_checked()
    except Exception:
        restore_transaction(kit, backup_path, start=running)
        raise KitError("凭据修改失败，已从备份恢复。")
    print("代理凭据已更新。密码不会在普通日志中显示；可使用 show-secret proxy 主动查看。")


def update(kit, image=None):
    target = image or kit.settings["image"]
    validate_image(target)
    path, running = kit.backup()
    old_image = kit.settings["image"]
    kit.settings["image"] = target
    try:
        kit.write_env()
        kit.compose("config", "--quiet")
        kit.compose("pull", "server", timeout=900)
    except Exception:
        kit.settings["image"] = old_image
        kit.write_env()
        raise
    kit.save()
    try:
        kit.up_checked()
    except Exception:
        restore_transaction(kit, path, start=running)
        raise KitError("镜像更新验证失败，已恢复更新前配置和数据。")
    print("镜像已检查并启动：" + target)
    print("使用固定版本，不会自动切换 latest，也不会单独替换上游内置内核。")


def published_ports():
    ids = process(["docker", "ps", "-q"]).split()
    if not ids:
        return set()
    raw = process(["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}"] + ids)
    ports = set()
    for line in raw.splitlines():
        for bindings in (json.loads(line) or {}).values():
            ports.update(int(p["HostPort"]) for p in (bindings or []))
    return ports


def environment_check():
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64", "aarch64", "arm64"):
        raise KitError("安装器支持 Linux amd64/arm64；建议 Ubuntu 24.04。")
    if os.geteuid() != 0:
        raise KitError("请以 root 或 sudo 运行安装器。")
    need_yaml()
    if not shutil.which("docker"):
        raise KitError("缺少 Docker。请自行安装 Docker Engine 及 Compose 插件。")
    engine = process(["docker", "version", "--format", "{{.Server.Version}}"])
    compose = process(["docker", "compose", "version", "--short"])
    print("环境：Linux %s，Docker %s，Compose %s" % (platform.machine(), engine.strip(), compose.strip()))


def validate_root(root):
    proposed = Path(root).expanduser()
    if proposed.is_symlink():
        raise KitError("安装目录不能是符号链接。")
    root = proposed.resolve()
    if len(root.parts) < 3 or root in (Path("/etc"), Path("/usr/local"), Path("/var/lib"), Path("/opt/accountmate"), Path("/opt/edge-proxy")):
        raise KitError("请选择独立的项目子目录，例如 /opt/metacubexd。")
    return root


def install(root):
    require_tty()
    environment_check()
    root = validate_root(root)
    if (root / "settings.json").is_file():
        existing = Kit(root)
        print("识别到本套件已有部署：复用配置、密钥和持久化数据，不重新初始化。")
        with deployment_lock(root):
            if not existing.settings.get("initialized"):
                print("此前初始化未完成，将使用原配置继续初始化，不更换已有密钥。")
                existing.bootstrap()
            existing.report()
        return
    # 识别旧部署时只读取归属和状态，不对不属于本套件的服务执行 up/down。
    legacy = process(["docker", "ps", "-a", "--filter", "name=^/metacubexd-server$", "--format", "{{.Names}} {{.Status}}"])
    if legacy.strip():
        print("发现已有核心：" + legacy.strip())
        raise KitError("已有核心不是本套件管理的部署，已停止初始化。请先审查原 Compose、挂载及密钥，不会覆盖或重建。")
    occupied_project = process(["docker", "ps", "-a", "--filter", "label=com.docker.compose.project=metacubexd", "--format", "{{.Names}}"])
    if occupied_project.strip():
        raise KitError("已有 metacubexd Compose 项目，未覆盖。")
    if any((root / name).exists() for name in (".env", "data")):
        raise KitError("目标目录已有环境配置或数据，拒绝当成新安装覆盖。")
    if root != SOURCE and root.exists() and any(root.iterdir()):
        raise KitError("新的安装目标须为空目录，或直接在克隆的仓库目录安装。")
    public = confirm("允许外部设备通过账号密码使用代理？", True)
    if public:
        print("普通 HTTP/SOCKS5 代理认证不加密客户端到代理服务器的连接；本项目不配置 TLS。")
    proxy_port = port_number(ask("代理端口", "9092"))
    user = ask("代理用户名")
    password = new_password()
    validate_credentials(user, password)
    control_port, api_port = 9090, 9091
    if confirm("修改高级设置（安装目录、面板及 API 端口）？"):
        new_root = validate_root(ask("安装目录", str(root)))
        if new_root != root:
            if new_root.exists() and any(new_root.iterdir()):
                raise KitError("新的安装目标须为空目录；尚未写入配置。")
            root = new_root
        control_port = port_number(ask("面板端口（仅本机）", "9090"))
        api_port = port_number(ask("API 端口（仅本机）", "9091"))
    s = {"schema": SCHEMA, "kit": "mihomo-deploy-kit", "project": "metacubexd", "image": IMAGE,
         "control_port": control_port, "api_port": api_port, "proxy_port": proxy_port,
         "proxy_bind": "0.0.0.0" if public else "127.0.0.1", "proxy_user": user,
         "proxy_password": password, "control_token": secrets.token_hex(32), "clash_secret": secrets.token_hex(32),
         "subscriptions": []}
    kit = Kit(root, s)
    published = published_ports()
    for port in (control_port, api_port, proxy_port):
        if port in published:
            raise KitError("端口已被 Docker 容器发布：%s" % port)
        free_port("0.0.0.0", port)
    parent = root if root.exists() else root.parent
    if not parent.exists() or shutil.disk_usage(parent).free < 1024 ** 3:
        raise KitError("父目录不存在或可用空间不足 1 GiB。")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    with deployment_lock(root):
        finish_install(kit)


def finish_install(kit):
    root, s = kit.root, kit.settings
    if (root / "settings.json").exists() or (root / "data").exists() or (root / ".env").exists():
        raise KitError("安装过程中发现已有配置，停止以免并发覆盖。")
    for name in PROGRAM_FILES:
        if root != SOURCE:
            private_write(root / name, (SOURCE / name).read_bytes())
    (root / "data").mkdir(mode=0o700)
    kit.save()
    kit.write_env(bind="127.0.0.1")
    baseline = dict(policy(s), **{"mode": "rule", "proxies": [], "proxy-groups": [], "rules": ["MATCH,REJECT"]})
    private_write(root / "data/active.yaml", dump_yaml(baseline))
    try:
        print("拉取固定版本官方镜像；首次启动只绑定本机，认证验证通过后才应用对外设置。")
        kit.bootstrap()
    except Exception:
        with contextlib.suppress(Exception):
            kit.stop()
        kit.write_env(bind="127.0.0.1")
        raise KitError("安装启动阶段失败：已停止本项目容器并恢复仅本机绑定；配置、数据均保留。运行 manage.sh logs/doctor 排查。")
    if confirm("现在导入订阅？"):
        import_subscription(kit)
    else:
        print("尚未导入订阅：初始配置拒绝出站请求。可稍后导入，或在面板配置服务器直连规则。")
    kit.doctor()
    kit.report()


HELP = """中文管理命令（默认目录 /opt/metacubexd）：
  help                       显示帮助
  status                     容器、内核、端口、数据目录
  doctor                     配置、挂载、网络及真实认证检查
  logs [-f]                  查看脱敏日志，-f 持续跟踪
  start / stop / restart     仅操作本项目容器
  update [--image 官方版本]   先备份，拉取镜像并验证；失败恢复
  backup                     短暂停止本项目并创建完整校验备份
  restore [备份路径]          验证备份，二次确认，先备份当前状态
  import-subscription        隐藏输入 URL、下载、校验、可选激活
  refresh-subscription       更新所选订阅，保留此前配置
  switch-profile             选择并激活已有配置
  change-proxy-auth          修改代理账号密码并校验
  show-secret [api|control|proxy]  警告并确认后在终端显示凭据
  uninstall                  备份后仅删除本项目容器，保留配置和数据
反向代理、域名、证书、防火墙由用户自行管理。"""


def main():
    parser = argparse.ArgumentParser(description="Mihomo + MetaCubeXD 轻量部署套件")
    parser.add_argument("--root", default=None, help="安装目录")
    parser.add_argument("command", nargs="?", default="help")
    parser.add_argument("argument", nargs="?")
    parser.add_argument("--image")
    parser.add_argument("-f", "--follow", action="store_true")
    args = parser.parse_args()
    if args.command == "help":
        print(HELP)
        return
    if args.command == "install":
        install(args.root or "/opt/metacubexd")
        return
    root = Path(args.root or SOURCE).resolve()
    if not (root / "settings.json").is_file():
        raise KitError("目录没有本套件部署配置，请先运行 install.sh。")
    need_yaml()
    if platform.system() != "Linux" or os.geteuid() != 0:
        raise KitError("管理操作请在 Linux 服务器上以 root 或 sudo 运行。")
    kit = Kit(root)
    with deployment_lock(root):
        dispatch(kit, args)


def dispatch(kit, args):
    if args.command == "status":
        kit.status()
    elif args.command == "doctor":
        kit.doctor()
    elif args.command == "logs":
        if args.argument not in (None, "core"):
            raise KitError("只有一个核心容器，没有独立反向代理容器。")
        kit.logs(args.follow)
    elif args.command in ("start", "restart"):
        if args.argument not in (None, "core"):
            raise KitError("只能操作本项目核心。")
        if args.command == "restart":
            kit.stop()
        kit.up_checked()
        kit.report()
    elif args.command == "stop":
        kit.stop()
    elif args.command == "backup":
        kit.backup()
    elif args.command == "restore":
        restore_interactive(kit, args.argument)
    elif args.command == "update":
        update(kit, args.image)
    elif args.command == "import-subscription":
        import_subscription(kit)
    elif args.command == "refresh-subscription":
        import_subscription(kit, refresh=True)
    elif args.command == "switch-profile":
        choose_profile(kit)
    elif args.command == "change-proxy-auth":
        change_auth(kit)
    elif args.command == "show-secret":
        require_tty()
        print("警告：下方会显示敏感凭据，请避免录屏、共享终端或复制到普通日志。")
        if input("确认显示请输入 SHOW：").strip() != "SHOW":
            return
        target = args.argument or "api"
        if target == "proxy":
            print("代理用户名：%s\n代理密码：%s" % (kit.settings["proxy_user"], kit.settings["proxy_password"]))
        elif target in ("api", "control"):
            print(kit.settings["clash_secret" if target == "api" else "control_token"])
        else:
            raise KitError("凭据类型须为 api、control 或 proxy。")
    elif args.command == "uninstall":
        if confirm("确认备份后移除本项目容器？配置、数据及网络均保留。"):
            kit.backup(resume=False)
            kit.inspect()
            kit.compose("rm", "-f", "server")
            print("本项目容器已移除。配置、数据、备份和网络均保留；没有操作其他项目。")
    else:
        raise KitError("未知命令。运行 manage.sh help 查看用法。")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except KeyboardInterrupt:
        print("\n操作中断，已有数据保留；请用 status/doctor 检查状态。", file=sys.stderr)
        sys.exit(130)
    except KitError as error:
        print("错误：" + str(error), file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print("错误：操作失败（%s），已有数据保留。请检查配置、权限和备份；未输出可能含凭据的异常内容。" % type(error).__name__, file=sys.stderr)
        sys.exit(1)
