import hashlib
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import updater  # noqa: E402


def tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.install = self.root / "apps/S2Arm"
        (self.install / "Engine").mkdir(parents=True)
        (self.install / "S2Arm.exe").write_text("old")
        self.private = self.root / "data"
        for name, body in (("DefaultLibrary/games/a/cover.jpg", "game"), ("keys/prod.keys", "k"), ("saves/backup.zip", "save"), ("jobs/t.json", "{}"), ("cache/x", "c")):
            path = self.private / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        (self.private / "updates").mkdir()
        self.staged = self.root / "staged"
        (self.staged / "Engine").mkdir(parents=True)
        (self.staged / "S2Arm.exe").write_text("new")

    def data(self):
        return {k: v for k, v in tree(self.private).items() if not k.startswith("updates")}


class ApplyTests(Base):
    def run_apply(self, launch, **options):
        return updater.apply(self.staged, self.install, 2147483646, self.private, launch=launch, health_seconds=options.pop("health", 1), **options)

    def healthy(self, install, args):
        (self.private / "updates/health.ok").write_text("ok")

    def test_success_replaces_install_and_keeps_every_user_file(self):
        before = self.data()
        self.assertEqual(self.run_apply(self.healthy), 0)
        self.assertEqual((self.install / "S2Arm.exe").read_text(), "new")
        self.assertFalse(self.install.with_name("S2Arm.s2arm-old").exists())
        self.assertEqual(self.data(), before)

    def test_read_only_files_do_not_leave_the_old_copy_behind(self):
        locked = self.install / "Engine/pack"; locked.write_text("x"); locked.chmod(0o444)
        self.assertEqual(self.run_apply(self.healthy), 0)
        self.assertFalse(self.install.with_name("S2Arm.s2arm-old").exists())

    def test_new_version_that_never_starts_is_rolled_back(self):
        before = self.data(); launches = []
        self.assertEqual(self.run_apply(lambda i, a: launches.append((i / "S2Arm.exe").read_text())), 1)
        self.assertEqual((self.install / "S2Arm.exe").read_text(), "old")
        self.assertEqual(launches, ["new", "old"])
        self.assertEqual(json.loads((self.private / "updates/last_failure.json").read_text())["code"], "update_rolled_back")
        self.assertEqual(self.data(), before)

    def test_swap_failure_keeps_old_version(self):
        real, calls = os.rename, []

        def flaky(source, target):
            calls.append(target)
            if str(target) == str(self.install) and len(calls) == 2:
                raise PermissionError(13, "locked")
            real(source, target)
        launches = []
        with patch.object(updater.os, "rename", flaky), patch.object(updater.time, "sleep", lambda s: None), patch.object(updater.time, "monotonic", side_effect=iter(range(0, 10**6, 100)).__next__):
            self.assertEqual(self.run_apply(lambda i, a: launches.append((i / "S2Arm.exe").read_text())), 1)
        self.assertEqual((self.install / "S2Arm.exe").read_text(), "old")
        self.assertEqual(launches, ["old"])

    def test_app_that_does_not_exit_is_left_alone(self):
        result = updater.apply(self.staged, self.install, os.getpid(), self.private, launch=self.fail, exit_seconds=0)
        self.assertEqual(result, 1)
        self.assertEqual((self.install / "S2Arm.exe").read_text(), "old")


class Handler(http.server.BaseHTTPRequestHandler):
    routes = {}
    hits = []

    def do_GET(self):
        self.hits.append(self.path)
        body = self.routes.get(self.path)
        self.send_response(200 if body is not None else 404); self.end_headers()
        self.wfile.write(body or b"")

    def log_message(self, *a):
        pass


class PrepareTests(Base):
    def setUp(self):
        super().setUp()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        patcher = patch.dict(os.environ, {"S2A_UPDATE_API": self.base + "/latest"})
        patcher.start(); self.addCleanup(patcher.stop)
        package = self.root / "p.zip"
        with zipfile.ZipFile(package, "w") as z:
            z.writestr("S2Arm/S2Arm.exe", "new"); z.writestr("S2Arm/Engine/e", "e"); z.writestr("S2Arm/Runtime/python/python.exe", "py")
            z.writestr("S2Arm/Runtime/python/Lib/site-packages/big", "x"); z.writestr("S2Arm/Runtime/python/Lib/os.py", "os")
        self.zip = package.read_bytes()
        self.publish(hashlib.sha256(self.zip).hexdigest())

    def publish(self, digest, tag="v9.9.9"):
        name = "S2Arm-windows-x64.zip"
        Handler.routes = {"/latest": json.dumps({"tag_name": tag, "assets": [
            {"name": name, "size": len(self.zip), "browser_download_url": self.base + "/pkg"},
            {"name": "SHA256SUMS.txt", "size": 80, "browser_download_url": self.base + "/sums"}]}).encode(),
            "/pkg": self.zip, "/sums": f"{digest}  {name}\n".encode()}

    def prepare(self, protect=()):
        with patch("builtins.print") as out, patch.object(updater.subprocess, "run", return_value=type("R", (), {"returncode": 0, "stdout": ""})()):
            updater.prepare("windows", "0.1.1", self.private, self.install, list(protect) + [self.private])
        return json.loads(out.call_args.args[0])

    def test_ready_leaves_install_untouched_and_runner_has_no_site_packages(self):
        before = tree(self.install)
        ready = self.prepare()
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(tree(self.install), before)
        self.assertEqual((Path(ready["staged"]) / "S2Arm.exe").read_text(), "new")
        self.assertTrue(Path(ready["python"]).is_file())
        self.assertFalse(list(Path(ready["python"]).parent.rglob("site-packages")))

    def test_checksum_mismatch_deletes_download_and_keeps_old(self):
        self.publish("0" * 64); before = tree(self.install)
        with self.assertRaises(updater.UpdateError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "update_checksum_mismatch")
        self.assertEqual(tree(self.install), before)
        self.assertFalse(list((self.private / "updates").rglob("S2Arm-windows-x64.zip")))

    def test_disk_full_stops_before_download(self):
        with patch.object(updater.shutil, "disk_usage", return_value=type("U", (), {"free": 1})()), patch.object(updater, "download", side_effect=AssertionError):
            with self.assertRaises(updater.UpdateError) as caught:
                self.prepare()
        self.assertEqual(caught.exception.code, "update_disk_full")

    def test_network_failure_is_reported_by_main_and_keeps_old(self):
        before = tree(self.install)
        Handler.routes["/pkg"] = None
        with patch("builtins.print") as out:
            code = updater.main(["prepare", "--platform", "windows", "--current", "0.1.1", "--private-root", str(self.private), "--install", str(self.install)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.call_args.args[0])["state"], "failed")
        self.assertEqual(tree(self.install), before)

    def test_data_inside_install_is_refused(self):
        inside = self.install / "MyLibrary"; inside.mkdir()
        with self.assertRaises(updater.UpdateError) as caught:
            self.prepare([inside])
        self.assertEqual(caught.exception.code, "update_data_inside_install")

    def test_zip_slip_is_refused(self):
        evil = self.root / "evil.zip"
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("../../escape", "x")
        target = self.root / "out"; target.mkdir()
        with self.assertRaises(updater.UpdateError):
            updater.extract_windows(evil, target)
        self.assertFalse((self.root / "escape").exists())


class DeltaTests(Base):
    def setUp(self):
        super().setUp()
        self.old = self.root / "old"; self.new = self.root / "new"
        for root, body in ((self.old, "v1"), (self.new, "v2")):
            (root / "Engine").mkdir(parents=True); (root / "same").write_text("same")
            (root / "tool").write_text(body); (root / "Engine/data").write_text(body * 1000)
        (self.old / "gone").write_text("x")
        (self.new / "added").write_text("new file")
        if os.name != "nt":
            (self.new / "tool").chmod(0o755); (self.old / "tool").chmod(0o644)
            os.symlink("tool", self.new / "alias")
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        patcher = patch.dict(os.environ, {"S2A_UPDATE_API": self.base + "/latest"}); patcher.start(); self.addCleanup(patcher.stop)
        self.delta = updater.build_delta(self.old, self.new, "windows", "0.1.1", "9.9.9", self.root)
        full = self.root / "full.zip"
        with zipfile.ZipFile(full, "w") as z:
            z.writestr("S2Arm/S2Arm.exe", "full"); z.writestr("S2Arm/Engine/e", "e"); z.writestr("S2Arm/Runtime/python/python.exe", "py")
        self.full = full.read_bytes()
        self.hits = Handler.hits = []

    def publish(self, delta):
        names = {"full": "S2Arm-windows-x64.zip", "delta": self.delta.name}
        sums = f"{hashlib.sha256(self.full).hexdigest()}  {names['full']}\n{hashlib.sha256(delta).hexdigest()}  {names['delta']}\n"
        Handler.routes = {"/latest": json.dumps({"tag_name": "v9.9.9", "assets": [
            {"name": names["full"], "size": len(self.full), "browser_download_url": self.base + "/full"},
            {"name": names["delta"], "size": len(delta), "browser_download_url": self.base + "/delta"},
            {"name": "SHA256SUMS.txt", "size": 9, "browser_download_url": self.base + "/sums"}]}).encode(),
            "/full": self.full, "/delta": delta, "/sums": sums.encode()}

    def prepare(self):
        shutil_rmtree = __import__("shutil").rmtree
        shutil_rmtree(self.install); __import__("shutil").copytree(self.old, self.install, symlinks=True)
        # 差分打完的目录里要有 Runtime/python 才能备 runner：清单里没有，打桩跳过
        with patch("builtins.print") as out, patch.object(updater, "runner_python", return_value=self.root / "py"), patch.object(updater.subprocess, "run", return_value=type("R", (), {"returncode": 0, "stdout": ""})()):
            updater.prepare("windows", "0.1.1", self.private, self.install, [self.private])
        return [json.loads(c.args[0]) for c in out.call_args_list]

    def test_delta_applies_to_a_copy_and_matches_the_new_tree_exactly(self):
        out = self.root / "out"; out.mkdir()
        staged = updater.apply_delta(self.delta, "windows", "0.1.1", "9.9.9", self.old, out)
        self.assertEqual(updater.file_manifest(staged), updater.file_manifest(self.new))
        self.assertTrue((self.old / "gone").exists())   # 原目录不动

    def test_only_changed_files_are_packed(self):
        with zipfile.ZipFile(self.delta) as z:
            self.assertNotIn("files/same", z.namelist())
            self.assertIn("files/tool", z.namelist())

    def test_prepare_uses_delta_and_skips_the_full_package(self):
        self.publish(self.delta.read_bytes()); before = self.data()
        events = self.prepare()
        self.assertEqual(events[-1]["via"], "delta")
        self.assertNotIn("/full", Handler.hits)
        self.assertEqual(updater.file_manifest(events[-1]["staged"]), updater.file_manifest(self.new))
        self.assertEqual(self.data(), before)

    def test_corrupt_delta_falls_back_to_full(self):
        self.publish(b"not a zip")
        events = self.prepare()
        self.assertIn("deltaFailed", [e["state"] for e in events])
        self.assertEqual(events[-1]["via"], "full")
        self.assertEqual((Path(events[-1]["staged"]) / "S2Arm.exe").read_text(), "full")

    def test_changed_install_falls_back_to_full(self):
        out = self.root / "out"; out.mkdir()
        (self.old / "same").write_text("edited by something")
        with self.assertRaises(updater.UpdateError):
            updater.apply_delta(self.delta, "windows", "0.1.1", "9.9.9", self.old, out)

    def test_delta_for_another_version_is_refused(self):
        out = self.root / "out"; out.mkdir()
        with self.assertRaises(updater.UpdateError):
            updater.apply_delta(self.delta, "windows", "0.1.0", "9.9.9", self.old, out)


class RunnerTests(unittest.TestCase):
    def test_apply_needs_no_network_modules(self):
        """updates/runner 的 Python 在 Mac 上没有 ssl：导入 updater 不能带进来。"""
        code = "import sys; sys.path.insert(0, %r); import updater; print([m for m in ('ssl', 'urllib.request', 'zipfile', '_hashlib') if m in sys.modules])" % str(Path(updater.__file__).parent)
        self.assertEqual(__import__("subprocess").run([sys.executable, "-I", "-c", code], capture_output=True, text=True).stdout.strip(), "[]")


class VersionTests(Base):
    def release(self, tag):
        body = {"tag_name": tag, "assets": [{"name": updater.ASSETS["mac"], "size": 1, "browser_download_url": "https://github.com/a"},
                                            {"name": updater.SUMS, "size": 1, "browser_download_url": "https://github.com/b"}]}
        return patch.object(updater, "open_url", return_value=type("R", (), {"__enter__": lambda s: __import__("io").BytesIO(json.dumps(body).encode()), "__exit__": lambda s, *a: None})())

    def test_only_a_strictly_higher_numeric_version_is_offered(self):
        for tag, current, available in (("v0.1.2", "0.1.1", True), ("v0.10.0", "0.9.9", True), ("v0.1.1", "0.1.1", False), ("v0.1.0", "0.1.1", False), ("v0.2.0-beta", "0.1.1", False), ("v0.2.0", "", False)):
            with self.subTest(tag=tag, current=current), self.release(tag):
                self.assertEqual(updater.latest("mac", current)["available"], available)

    def test_official_build_refuses_other_hosts(self):
        with patch.dict(os.environ, {"S2A_UPDATE_API": ""}):
            self.assertFalse(updater.trusted("https://evil.example/x"))
            self.assertFalse(updater.trusted("http://github.com/x"))
            self.assertTrue(updater.trusted("https://github.com/x"))


if __name__ == "__main__":
    unittest.main()
