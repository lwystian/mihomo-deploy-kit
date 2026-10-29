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

    def test_password_is_hidden_and_entered_once(self):
        with mock.patch.object(kit, "require_tty"), mock.patch.object(kit.getpass, "getpass", return_value="1") as password:
            self.assertEqual(kit.new_password(), "1")
            self.assertEqual(password.call_count, 1)

    def test_show_secret_prints_requested_value_without_confirmation(self):
        for target, expected in (("api", self.instance.settings["clash_secret"]), ("control", self.instance.settings["control_token"]), ("proxy", self.instance.settings["proxy_password"])):
            with self.subTest(target=target), mock.patch("builtins.input", side_effect=AssertionError("unexpected confirmation")), mock.patch.object(kit, "require_tty", side_effect=AssertionError("unexpected terminal gate")), contextlib.redirect_stdout(io.StringIO()) as output:
                args = type("Args", (), {"command": "show-secret", "argument": target})()
                kit.dispatch(self.instance, args)
            self.assertIn(expected, output.getvalue())
            self.assertNotIn("SHOW", output.getvalue())
            self.assertNotIn("警告", output.getvalue())

    def test_restore_with_path_stages_source_before_rotating_backup(self):
        archive = self.root.parent / "saved.tar.gz"
        with mock.patch.object(kit, "confirm", side_effect=AssertionError("unexpected confirmation")), mock.patch.object(kit, "require_tty", side_effect=AssertionError("unexpected terminal gate")), mock.patch.object(kit, "restore_transaction") as restore:
            kit.restore_interactive(self.instance, str(archive))
        restore.assert_called_once_with(self.instance, archive, backup_current=True)

    def test_backup_retains_one_complete_snapshot_inside_project(self):
        with mock.patch.object(self.instance, "running", return_value=False):
            first, _ = self.instance.backup()
            kit.private_write(self.root / "data/marker", "newest")
            latest, _ = self.instance.backup()
        self.assertEqual(latest.parent, self.root / "backups")
        self.assertFalse(first.exists())
        self.assertFalse(Path(str(first) + ".sha256").exists())
        self.assertEqual(list(latest.parent.glob("*.tar.gz")), [latest])
        kit.verify_archive(latest)
        with tarfile.open(latest) as archive:
            self.assertEqual(archive.extractfile("data/marker").read(), b"newest")
            self.assertFalse(any(n.startswith("backups/") for n in archive.getnames()))

    def test_failed_backup_preserves_previous_verified_snapshot(self):
        with mock.patch.object(self.instance, "running", return_value=False):
            previous, _ = self.instance.backup()
            def fail(root, destination):
                kit.private_write(destination, "incomplete")
                raise kit.KitError("test archive failure")
            with mock.patch.object(kit, "create_archive", side_effect=fail):
                with self.assertRaises(kit.KitError):
                    self.instance.backup()
        kit.verify_archive(previous)
        self.assertEqual(list(previous.parent.glob("*.tar.gz")), [previous])

    def test_successful_backup_retires_known_legacy_directory(self):
        legacy = self.root.parent / (self.root.name + "-backups")
        legacy.mkdir()
        old = legacy / "20200101T000000000000Z.tar.gz"
        kit.create_archive(self.root, old)
        kit.private_write(str(old) + ".sha256", kit.digest_file(old) + "  " + old.name + "\n")
        code = legacy / "code-20200101T000000000001Z.tar.gz"
        with tarfile.open(code, "w:gz") as archive:
            for name in kit.PROGRAM_FILES:
                archive.add(self.root / name, arcname=name)
        kit.private_write(str(code) + ".sha256", kit.digest_file(code) + "  " + code.name + "\n")
        with mock.patch.object(self.instance, "running", return_value=False):
            latest, _ = self.instance.backup()
        self.assertTrue(latest.is_file())
        self.assertFalse(legacy.exists())

    def test_backup_rotation_leaves_unrecognized_files_alone(self):
        directory = self.root / "backups"
        directory.mkdir()
        kit.private_write(directory / "notes.txt", "keep")
        kit.private_write(directory / "20200101T000000000000Z.tar.gz", "unknown")
        with mock.patch.object(self.instance, "running", return_value=False):
            self.instance.backup()
        self.assertEqual((directory / "notes.txt").read_text(), "keep")
        self.assertEqual((directory / "20200101T000000000000Z.tar.gz").read_text(), "unknown")

    def test_restore_from_only_backup_keeps_pre_restore_state_as_latest_backup(self):
        marker = self.root / "data/marker"
        kit.private_write(marker, "old state")
        with mock.patch.object(self.instance, "running", return_value=False):
            source, _ = self.instance.backup()
            kit.private_write(marker, "current state")
            with mock.patch.object(kit.Kit, "compose"), mock.patch.object(kit.Kit, "stop"):
                kit.restore_interactive(self.instance, str(source))
        self.assertEqual(marker.read_text(), "old state")
        self.assertFalse(source.exists())
        backups = list((self.root / "backups").glob("*.tar.gz"))
        self.assertEqual(len(backups), 1)
        with tarfile.open(backups[0]) as archive:
            self.assertEqual(archive.extractfile("data/marker").read(), b"current state")

    def test_uninstall_backs_up_and_only_removes_own_container_without_confirmation(self):
        with mock.patch.object(kit, "confirm", side_effect=AssertionError("unexpected confirmation")), mock.patch.object(self.instance, "backup") as backup, mock.patch.object(self.instance, "inspect"), mock.patch.object(self.instance, "compose") as compose:
            kit.dispatch(self.instance, type("Args", (), {"command": "uninstall"})())
        backup.assert_called_once_with(resume=False)
        compose.assert_called_once_with("rm", "-f", "server")
        self.assertTrue((self.root / "data/active.yaml").is_file())

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
                 mock.patch.object(kit, "sync_code", return_value=False), \
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


@unittest.skipUnless(shutil.which("git"), "代码更新测试需要 Git")
class CodeUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.remote, self.seed, self.root = base / "remote.git", base / "seed", base / "project"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        self.seed.mkdir()
        self.git(self.seed, "init", "-b", "main")
        self.git(self.seed, "config", "user.name", "Deployment Test")
        self.git(self.seed, "config", "user.email", "test@example.invalid")
        for name in kit.PROGRAM_FILES + ("deploy.sh", ".gitignore", ".gitattributes"):
            shutil.copy2(ROOT / name, self.seed / name)
        self.commit("initial")
        self.git(self.seed, "remote", "add", "origin", str(self.remote))
        self.git(self.seed, "push", "-u", "origin", "main")
        subprocess.run(["git", "clone", "-b", "main", str(self.remote), str(self.root)],
                       check=True, capture_output=True)
        self.before = self.git(self.root, "rev-parse", "HEAD").strip()

    def git(self, root, *arguments):
        result = subprocess.run(["git", "-c", "safe.directory=" + root.as_posix()] + list(arguments),
                                cwd=root, check=True, capture_output=True, text=True, encoding="utf-8")
        return result.stdout

    def commit(self, message):
        self.git(self.seed, "add", "-A")
        self.git(self.seed, "commit", "-m", message)

    def publish(self, name="README.md", content="new code\n"):
        (self.seed / name).write_text(content, encoding="utf-8")
        self.commit("remote change")
        self.git(self.seed, "push")

    def test_fast_forward_preserves_private_data_and_code_history_without_extra_archives(self):
        private = {".env": b"KEEP-SECRET", "settings.json": b"KEEP-SETTINGS", "data/active.yaml": b"KEEP-DATA"}
        for name, content in private.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_bytes(content)
        self.publish()
        self.assertTrue(kit.sync_code(self.root))
        self.assertNotEqual(self.git(self.root, "rev-parse", "HEAD").strip(), self.before)
        for name, content in private.items():
            self.assertEqual((self.root / name).read_bytes(), content)
        self.assertFalse((self.root / "backups").exists())
        self.assertFalse((self.root.parent / "project-backups").exists())
        self.assertEqual(self.git(self.root, "show", self.before + ":README.md"), (ROOT / "README.md").read_text("utf-8"))

    def test_no_remote_change_does_not_make_code_backup(self):
        self.assertFalse(kit.sync_code(self.root))
        self.assertFalse((self.root.parent / "project-backups").exists())

    @unittest.skipUnless(os.name == "posix", "恢复后的 Git 可执行位在 Linux CI 验证")
    def test_restored_scripts_keep_executable_bits_and_clean_git_state(self):
        instance = deployment(self.root)
        for name in ("install.sh", "manage.sh"):
            os.chmod(self.root / name, 0o700)
        archive, staging = self.root.parent / "backup.tar.gz", self.root.parent / "staging"
        kit.create_archive(instance.root, archive)
        staging.mkdir()
        kit.verify_archive(archive, staging)
        for name in kit.PROGRAM_FILES:
            os.replace(staging / name, self.root / name)
        self.assertEqual(self.git(self.root, "status", "--porcelain", "--untracked-files=no"), "")
        for name in ("install.sh", "manage.sh"):
            self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o700)

    def test_dirty_code_is_not_overwritten(self):
        self.publish()
        (self.root / "README.md").write_text("my local edits", encoding="utf-8")
        with self.assertRaisesRegex(kit.KitError, "本地修改"):
            kit.sync_code(self.root)
        self.assertEqual((self.root / "README.md").read_text(), "my local edits")
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD").strip(), self.before)

    def test_diverged_branch_is_not_reset_or_merged(self):
        self.publish()
        self.git(self.root, "config", "user.name", "Deployment Test")
        self.git(self.root, "config", "user.email", "test@example.invalid")
        (self.root / "local.txt").write_text("local commit", encoding="utf-8")
        self.git(self.root, "add", "local.txt")
        self.git(self.root, "commit", "-m", "local commit")
        local_head = self.git(self.root, "rev-parse", "HEAD")
        with self.assertRaisesRegex(kit.KitError, "快进"):
            kit.sync_code(self.root)
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), local_head)

    def test_untracked_collision_is_preserved(self):
        self.publish("extra.txt", "remote version")
        (self.root / "extra.txt").write_text("local version", encoding="utf-8")
        with self.assertRaisesRegex(kit.KitError, "快进失败"):
            kit.sync_code(self.root)
        self.assertEqual((self.root / "extra.txt").read_text(), "local version")
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD").strip(), self.before)

    def test_ignored_local_file_collision_is_preserved(self):
        (self.seed / "private.log").write_text("remote version", encoding="utf-8")
        self.git(self.seed, "add", "-f", "private.log")
        self.git(self.seed, "commit", "-m", "track ignored path")
        self.git(self.seed, "push")
        (self.root / "private.log").write_text("local private content", encoding="utf-8")
        with self.assertRaisesRegex(kit.KitError, "快进失败"):
            kit.sync_code(self.root)
        self.assertEqual((self.root / "private.log").read_text(), "local private content")

    def test_remote_runtime_files_are_rejected(self):
        (self.seed / ".env").write_text("remote secret", encoding="utf-8")
        self.git(self.seed, "add", "-f", ".env")
        self.git(self.seed, "commit", "-m", "unsafe runtime path")
        self.git(self.seed, "push")
        (self.root / ".env").write_text("private secret", encoding="utf-8")
        with self.assertRaisesRegex(kit.KitError, "运行配置或数据"):
            kit.sync_code(self.root)
        self.assertEqual((self.root / ".env").read_text(), "private secret")

    def test_invalid_new_python_is_rejected_before_merge(self):
        self.publish("kit.py", "invalid python !")
        with self.assertRaisesRegex(kit.KitError, "语法无效"):
            kit.sync_code(self.root)
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD").strip(), self.before)

    def test_missing_required_file_is_rejected(self):
        (self.seed / "deploy.sh").unlink()
        self.commit("remove entrypoint")
        self.git(self.seed, "push")
        with self.assertRaisesRegex(kit.KitError, "缺少必要"):
            kit.sync_code(self.root)
        self.assertTrue((self.root / "deploy.sh").is_file())

    def test_fetch_failure_does_not_change_code(self):
        self.git(self.root, "remote", "set-url", "origin", str(self.root.parent / "missing.git"))
        with self.assertRaisesRegex(kit.KitError, "拉取远程代码失败"):
            kit.sync_code(self.root)
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD").strip(), self.before)

    def test_nongit_directory_requires_explicit_image_only(self):
        plain = self.root.parent / "plain"
        plain.mkdir()
        with self.assertRaisesRegex(kit.KitError, "不是 Git"):
            kit.sync_code(plain)

    def test_update_before_install_skips_containers_and_reexecs_new_code(self):
        for changed in (False, True):
            with self.subTest(changed=changed), \
                 mock.patch.object(sys, "argv", ["deploy.sh", "--root", str(self.root), "update"]), \
                 mock.patch.object(kit.platform, "system", return_value="Linux"), \
                 mock.patch.object(kit.os, "geteuid", return_value=0, create=True), \
                 mock.patch.object(kit, "deployment_lock", return_value=contextlib.nullcontext()), \
                 mock.patch.object(kit, "sync_code", return_value=changed), \
                 mock.patch.object(kit, "reexec_update") as reexec, mock.patch.object(kit, "dispatch") as dispatch:
                kit.main()
                self.assertEqual(reexec.call_count, int(changed))
                dispatch.assert_not_called()

    def test_reexec_uses_updated_python_and_does_not_pull_recursively(self):
        with mock.patch.object(kit.os, "execv") as execute:
            kit.reexec_update(self.root, kit.IMAGE)
        execute.assert_called_once_with(sys.executable, [sys.executable, str(self.root / "kit.py"),
            "--root", str(self.root), "update", "--image-only", "--image", kit.IMAGE])


if __name__ == "__main__":
    unittest.main(verbosity=2)
