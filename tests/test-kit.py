#!/usr/bin/env python3
"""不调用 Docker、不访问真实服务器的本地验证。"""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import kit


def settings():
    return {"schema": 1, "kit": "mihomo-deploy-kit", "project": "mkit-test",
            "image": kit.IMAGE, "control_port": 19090, "api_port": 19091, "proxy_port": 19092,
            "proxy_bind": "127.0.0.1", "proxy_user": "test-user", "proxy_password": "Test-only-$'@pass:word\\123",
            "control_token": "a" * 64, "clash_secret": "b" * 64, "subscriptions": [], "initialized": True}


def deployment(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for name in kit.PROGRAM_FILES:
        (root / name).write_bytes((ROOT / name).read_bytes())
    instance = kit.Kit(root, settings())
    instance.save()
    instance.write_env()
    (root / "data/profiles").mkdir(parents=True)
    kit.private_write(root / "data/active.yaml", kit.dump_yaml(dict(kit.policy(instance.settings), rules=["MATCH,REJECT"])))
    kit.private_write(root / "data/profiles/preserved.yaml", "rules: [MATCH,DIRECT]\n")
    return instance


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.instance = deployment(self.root)

    def test_special_password_survives_yaml_and_never_enters_env(self):
        s = self.instance.settings
        parsed = kit.safe_yaml(kit.dump_yaml(kit.policy(s)))
        self.assertEqual(parsed["authentication"], [s["proxy_user"] + ":" + s["proxy_password"]])
        self.assertNotIn(s["proxy_password"], (self.root / ".env").read_text())
        kit.check_policy(parsed, s)

    def test_invalid_port_and_duplicate_ports_rejected(self):
        for number in (0, 80, 443, 65536, "abc"):
            with self.assertRaises(kit.KitError):
                kit.port_number(number)
        bad = settings()
        bad["api_port"] = bad["proxy_port"]
        with self.assertRaises(kit.KitError):
            kit.Kit(self.root, bad)

    def test_public_without_password_cannot_be_configured(self):
        bad = settings()
        bad.update(proxy_bind="0.0.0.0", proxy_password="")
        with self.assertRaises(kit.KitError):
            kit.Kit(self.root, bad)

    def test_short_simple_and_special_passwords_are_accepted(self):
        for password in ("1", "123", "password", " ", "密", " a:@$'\\ ", "x" * 255, "密" * 85):
            with self.subTest(length=len(password.encode("utf-8"))):
                value = settings()
                value.update(proxy_user="中文 user@example", proxy_password=password)
                instance = kit.Kit(self.root, value)
                roundtrip = kit.safe_yaml(kit.dump_yaml(kit.policy(value)))
                kit.check_policy(roundtrip, instance.settings)

    def test_password_only_rejects_unusable_protocol_values(self):
        for password in ("", "x" * 256, "密" * 86, "a\nb", "a\rb", "a\0b"):
            with self.assertRaises(kit.KitError):
                kit.validate_credentials("proxy", password)

    def test_password_mismatch_retries_only_the_password(self):
        output = io.StringIO()
        with mock.patch.object(kit, "require_tty"), \
             mock.patch.object(kit.getpass, "getpass", side_effect=["123", "456", "1", "1"]), \
             contextlib.redirect_stdout(output):
            self.assertEqual(kit.new_password(), "1")
        self.assertNotIn("123", output.getvalue())
        self.assertNotIn("456", output.getvalue())

    def test_blank_password_generates_or_preserves_existing_value(self):
        with mock.patch.object(kit, "require_tty"), mock.patch.object(kit.getpass, "getpass", return_value=""), \
             mock.patch.object(kit.secrets, "token_urlsafe", return_value="generated-only-for-test") as generated:
            self.assertEqual(kit.new_password(), "generated-only-for-test")
            self.assertEqual(kit.new_password(current="1"), "1")
            generated.assert_called_once()

    def test_blank_subscription_skips_download_and_backup(self):
        before = (self.root / "settings.json").read_bytes()
        with mock.patch.object(self.instance, "runtime_check"), mock.patch.object(self.instance, "ensure_overlay"), \
             mock.patch.object(kit, "require_tty"), mock.patch.object(kit.getpass, "getpass", return_value=""), \
             mock.patch.object(kit, "download_subscription") as download, mock.patch.object(self.instance, "backup") as backup:
            kit.import_subscription(self.instance)
            download.assert_not_called()
            backup.assert_not_called()
        self.assertEqual(before, (self.root / "settings.json").read_bytes())

    def test_blank_restore_and_profile_choice_cancel_without_backup(self):
        with mock.patch.object(kit, "require_tty"), mock.patch.object(kit, "ask", return_value=None), \
             mock.patch.object(self.instance, "request", return_value=[{"id": "old", "name": "原配置", "type": "local"}]), \
             mock.patch.object(self.instance, "backup") as backup, mock.patch.object(self.instance, "activate") as activate:
            kit.restore_interactive(self.instance)
            kit.choose_profile(self.instance)
            backup.assert_not_called()
            activate.assert_not_called()

    def test_unchanged_credentials_do_not_restart_or_backup(self):
        with mock.patch.object(kit, "ask", return_value=self.instance.settings["proxy_user"]), \
             mock.patch.object(kit, "new_password", return_value=self.instance.settings["proxy_password"]), \
             mock.patch.object(self.instance, "backup") as backup:
            kit.change_auth(self.instance)
            backup.assert_not_called()

    def test_default_install_uses_source_and_commands_route_to_management(self):
        with mock.patch.object(sys, "argv", ["deploy.sh"]), mock.patch.object(kit, "install") as install:
            kit.main()
            install.assert_called_once_with(kit.SOURCE)
        for command in ("update", "stop", "start", "restart", "status", "logs", "backup"):
            with mock.patch.object(sys, "argv", ["deploy.sh", "--root", str(self.root), command]), \
                 mock.patch.object(kit.platform, "system", return_value="Linux"), \
                 mock.patch.object(kit.os, "geteuid", return_value=0, create=True), \
                 mock.patch.object(kit, "deployment_lock", return_value=contextlib.nullcontext()), \
                 mock.patch.object(kit, "dispatch") as dispatch, mock.patch.object(kit, "install") as install:
                kit.main()
                self.assertEqual(dispatch.call_args[0][1].command, command)
                install.assert_not_called()

    def test_install_allows_enter_for_default_username_and_options(self):
        destination = Path(self.temp.name) / "any-parent" / "any-project"
        destination.parent.mkdir()
        recorded = {}
        def finish(instance):
            recorded.update(instance.settings)
        with mock.patch.object(kit, "require_tty"), mock.patch.object(kit, "environment_check"), \
             mock.patch.object(kit, "process", return_value=""), mock.patch.object(kit, "published_ports", return_value=set()), \
             mock.patch.object(kit, "free_port"), mock.patch("builtins.input", return_value=""), \
             mock.patch.object(kit.getpass, "getpass", return_value=""), \
             mock.patch.object(kit, "deployment_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(kit, "finish_install", side_effect=finish):
            kit.install(destination)
        self.assertEqual(recorded["proxy_user"], "proxy")
        self.assertEqual(recorded["proxy_port"], 9092)
        self.assertTrue(recorded["proxy_password"])

    def test_arbitrary_images_and_latest_rejected(self):
        for image in ("other/image:v1.0.0", "ghcr.io/metacubex/metacubexd-server:latest", "a; echo unsafe"):
            with self.assertRaises(kit.KitError):
                kit.validate_image(image)

    def test_safe_yaml_rejects_code_constructors(self):
        with self.assertRaises(kit.KitError):
            kit.safe_yaml("!!python/object/apply:os.system ['echo unsafe']")

    def test_authentication_bypass_and_wildcard_cors_rejected(self):
        for field, value in (("authentication", []), ("skip-auth-prefixes", ["0.0.0.0/0"]),
                             ("allow-lan", False), ("listeners", [{"type": "http", "port": 9092}]),
                             ("external-controller-cors", {"allow-origins": ["*"]})):
            bad = kit.policy(self.instance.settings)
            bad[field] = value
            with self.assertRaises(kit.KitError):
                kit.check_policy(bad, self.instance.settings)

    def test_local_overlay_allows_explicit_user_origin(self):
        value = kit.policy(self.instance.settings)
        value["external-controller-cors"]["allow-origins"] = ["https://panel.example.com"]
        kit.check_policy(value, self.instance.settings)

    def test_logs_hide_password_tokens_and_urls(self):
        s = self.instance.settings
        text = " ".join([s["proxy_password"], s["control_token"], s["clash_secret"], "https://example.com/sub?token=test-private"])
        result = self.instance.scrub(text)
        self.assertNotIn("test-private", result)
        for key in ("proxy_password", "control_token", "clash_secret"):
            self.assertNotIn(s[key], result)

    def test_backup_roundtrip_and_checksum(self):
        archive = Path(self.temp.name) / "backup.tar.gz"
        kit.create_archive(self.root, archive)
        output = Path(self.temp.name) / "restored"
        self.assertTrue(kit.verify_archive(archive, output))
        for name in ("settings.json", ".env", "data/active.yaml", "data/profiles/preserved.yaml"):
            self.assertEqual((self.root / name).read_bytes(), (output / name).read_bytes())
        if os.name == "posix":
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)

    def test_corrupted_backup_rejected_before_overwrite(self):
        archive = Path(self.temp.name) / "backup.tar.gz"
        damaged = Path(self.temp.name) / "damaged.tar.gz"
        kit.create_archive(self.root, archive)
        with tarfile.open(archive, "r:gz") as source, tarfile.open(damaged, "w:gz") as target:
            for member in source.getmembers():
                content = source.extractfile(member).read() if member.isfile() else None
                if member.name == "settings.json":
                    content += b"corruption"
                    member.size = len(content)
                target.addfile(member, io.BytesIO(content) if content is not None else None)
        before = (self.root / "settings.json").read_bytes()
        with self.assertRaises(kit.KitError):
            kit.verify_archive(damaged)
        self.assertEqual(before, (self.root / "settings.json").read_bytes())

    def test_path_traversal_and_links_rejected(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("data/link", tarfile.SYMTYPE), ("data/hard", tarfile.LNKTYPE)):
            archive = Path(self.temp.name) / "unsafe.tar.gz"
            with tarfile.open(archive, "w:gz") as target:
                member = tarfile.TarInfo(name)
                member.type, member.linkname = kind, "../../escape"
                target.addfile(member)
            with self.assertRaises(kit.KitError):
                kit.verify_archive(archive)

    def test_duplicate_archive_member_rejected(self):
        archive = Path(self.temp.name) / "duplicate.tar.gz"
        with tarfile.open(archive, "w:gz") as target:
            target.addfile(tarfile.TarInfo("data"))
            target.addfile(tarfile.TarInfo("data"))
        with self.assertRaises(kit.KitError):
            kit.verify_archive(archive)

    def test_bad_subscription_leaves_files_and_current_profile_unchanged(self):
        before = (self.root / "data/active.yaml").read_bytes()
        with mock.patch.object(self.instance, "runtime_check"), mock.patch.object(self.instance, "ensure_overlay"), \
             mock.patch.object(self.instance, "backup") as backup, mock.patch.object(kit, "require_tty"), \
             mock.patch.object(kit.getpass, "getpass", return_value="https://example.com/private?token=secret"), \
             mock.patch.object(kit, "ask", return_value="主订阅"), mock.patch.object(kit, "confirm", return_value=True), \
             mock.patch.object(kit, "download_subscription", side_effect=kit.KitError("下载失败")):
            with self.assertRaises(kit.KitError):
                kit.import_subscription(self.instance)
            backup.assert_not_called()
        self.assertEqual(before, (self.root / "data/active.yaml").read_bytes())

    def test_untrusted_script_overlay_rejected_without_execution(self):
        with mock.patch.object(self.instance, "request", return_value=[{"id": "script", "type": "script"}]):
            with self.assertRaises(kit.KitError):
                self.instance.ensure_overlay()

    def test_activation_failure_restores_previous_and_keeps_candidate(self):
        calls = []
        def request(path, *args, **kwargs):
            calls.append(path)
            if path.endswith("/editor"):
                return {"composedYaml": kit.dump_yaml(kit.policy(self.instance.settings))}
            if path == "/profiles":
                return [{"id": "old", "active": True}]
            if path == "/profiles/new/activate":
                raise kit.KitError("候选启动失败")
            return {"status": "running"}
        with mock.patch.object(self.instance, "request", side_effect=request), \
             mock.patch.object(self.instance, "ensure_overlay"), mock.patch.object(self.instance, "ready"), \
             mock.patch.object(self.instance, "runtime_check"), mock.patch.object(self.instance, "stop") as stop:
            with self.assertRaises(kit.KitError):
                self.instance.activate("new")
            stop.assert_not_called()
        self.assertIn("/profiles/old/activate", calls)
        self.assertFalse(any("delete" in path for path in calls))

    def test_failed_start_stops_own_container(self):
        with mock.patch.object(self.instance, "compose"), mock.patch.object(self.instance, "inspect"), \
             mock.patch.object(self.instance, "ready", side_effect=kit.KitError("不可用")), \
             mock.patch.object(self.instance, "stop") as stop:
            with self.assertRaises(kit.KitError):
                self.instance.up_checked()
            stop.assert_called_once()
        self.assertTrue((self.root / "data/profiles/preserved.yaml").exists())

    def test_restore_failure_rolls_back_files_without_deleting_either_data_set(self):
        archive = Path(self.temp.name) / "snapshot.tar.gz"
        kit.create_archive(self.root, archive)
        kit.private_write(self.root / "data/profiles/preserved.yaml", "before restore, keep me")
        with mock.patch.object(kit.Kit, "compose"), mock.patch.object(kit.Kit, "stop"), \
             mock.patch.object(kit.Kit, "up_checked", side_effect=[kit.KitError("候选失败"), None]):
            with self.assertRaises(kit.KitError):
                kit.restore_transaction(self.instance, archive)
        self.assertEqual((self.root / "data/profiles/preserved.yaml").read_text(), "before restore, keep me")
        self.assertTrue(list(self.root.glob(".failed-restore-*/data")))

    def test_port_collision_prevents_any_new_deployment_files(self):
        new_root = Path(self.temp.name) / "never-created"
        with mock.patch.object(kit, "require_tty"), mock.patch.object(kit, "environment_check"), \
             mock.patch.object(kit, "process", return_value=""), mock.patch.object(kit, "confirm", return_value=False), \
             mock.patch.object(kit, "ask", side_effect=["9092", "test-user"]), \
             mock.patch.object(kit, "new_password", return_value="Test-only-password-123"), \
             mock.patch.object(kit, "published_ports", return_value={9092}):
            with self.assertRaises(kit.KitError):
                kit.install(new_root)
        self.assertFalse(new_root.exists())

    def test_existing_managed_deployment_reuses_secrets_and_data(self):
        before = (self.root / "settings.json").read_bytes()
        with mock.patch.object(kit, "require_tty"), mock.patch.object(kit, "environment_check"), \
             mock.patch.object(kit, "deployment_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(kit.Kit, "report"), mock.patch.object(kit.secrets, "token_hex") as generated:
            kit.install(self.root)
            generated.assert_not_called()
        self.assertEqual(before, (self.root / "settings.json").read_bytes())

    def test_unknown_existing_core_is_never_recreated(self):
        new_root = Path(self.temp.name) / "unknown-existing"
        with mock.patch.object(kit, "require_tty"), mock.patch.object(kit, "environment_check"), \
             mock.patch.object(kit, "process", return_value="metacubexd-server Up (healthy)") as process:
            with self.assertRaises(kit.KitError):
                kit.install(new_root)
            self.assertEqual(len(process.call_args_list), 1)
        self.assertFalse(new_root.exists())

    def test_busy_port_is_detected(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            with self.assertRaises(kit.KitError):
                kit.free_port("0.0.0.0", listener.getsockname()[1])

    def test_compose_only_has_one_service_and_local_management_ports(self):
        value = kit.safe_yaml((ROOT / "compose.yaml").read_text("utf-8"))
        self.assertEqual(set(value["services"]), {"server"})
        ports = value["services"]["server"]["ports"]
        self.assertTrue(ports[0].startswith("127.0.0.1:"))
        self.assertTrue(ports[1].startswith("127.0.0.1:"))
        self.assertFalse(any("/udp" in port for port in ports))

    @unittest.skipUnless(os.name == "posix", "直接执行 .sh 及可执行位在 Linux CI 验证")
    def test_deploy_runs_from_project_directory_and_forwards_custom_root(self):
        project = Path(self.temp.name) / "项目 空格"
        project.mkdir()
        for name in ("deploy.sh", "install.sh"):
            shutil.copy2(ROOT / name, project / name)
            os.chmod(project / name, 0o700)
        binary = Path(self.temp.name) / "bin"
        binary.mkdir()
        python = binary / "python3"
        python.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
        os.chmod(python, 0o700)
        env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ["PATH"])
        for extra in ([], ["--root", "/srv/自定 目录"], ["update"], ["stop"], ["logs", "-f"], ["help"]):
            result = subprocess.run(["./deploy.sh"] + extra, cwd=project, env=env,
                                    text=True, encoding="utf-8", capture_output=True, check=True)
            self.assertEqual(result.stdout.splitlines(), [str(project / "kit.py"), "--root", str(project)] + extra)

    def test_custom_install_copies_deploy_entry_and_keeps_scripts_executable(self):
        destination = Path(self.temp.name) / "custom-install"
        destination.mkdir()
        instance = kit.Kit(destination, settings())
        with mock.patch.object(instance, "bootstrap"), mock.patch.object(instance, "doctor"), \
             mock.patch.object(instance, "report"), mock.patch.object(kit, "confirm", return_value=False):
            kit.finish_install(instance)
        self.assertEqual((destination / "deploy.sh").read_bytes(), (ROOT / "deploy.sh").read_bytes())
        if os.name == "posix":
            for name in ("deploy.sh", "install.sh", "manage.sh"):
                self.assertEqual((destination / name).stat().st_mode & 0o777, 0o700)

    def test_proxy_probe_understands_upstream_rejections_without_accepting_anonymous(self):
        def fake_socket(chunks):
            stream = mock.MagicMock()
            stream.__enter__.return_value = stream
            stream.recv.side_effect = chunks
            return stream
        streams = [fake_socket([b"HTTP/1.1 407 Proxy Authentication Required\r\n"]),
                   fake_socket([b"HTTP/1.1 403 Forbidden\r\n"]), fake_socket([b"\x05\x02"]),
                   fake_socket([b"\x05\x02", b"\x01\x01"]), fake_socket([b"\x05\x02", b"\x01\x00"])]
        with mock.patch.object(kit.socket, "create_connection", side_effect=streams):
            self.instance.proxy_auth_check()
        streams = [fake_socket([b"HTTP/1.1 407 Required\r\n"]),
                   fake_socket([b"HTTP/1.1 403 Forbidden\r\n"]), fake_socket([b"\x05\x00"])]
        with mock.patch.object(kit.socket, "create_connection", side_effect=streams):
            with self.assertRaises(kit.KitError):
                self.instance.proxy_auth_check()


if __name__ == "__main__":
    unittest.main(verbosity=2)
