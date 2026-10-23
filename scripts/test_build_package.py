#!/usr/bin/env python3
"""实际历史模块重打包与临时安装状态测试；不编译、加载或安装宿主驱动。"""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import build_package as package
import verify_package as verification

CORE_SOURCE = os.environ.get("FAST_DEPLOY_TEST_PACKAGE_CORE_ROOT")
EVIDENCE_FILE = os.environ.get("UGREEN_TEST_EVIDENCE_FILE")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def commit(root):
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-qm", "冻结驱动分发测试源码")
    return git(root, "rev-parse", "HEAD")


class PackageFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "driver"
        self.root.mkdir()
        shutil.copytree(package.ROOT / "scripts", self.root / "scripts",
                        ignore=shutil.ignore_patterns("__pycache__"))
        for name in (".gitignore", "release.json", "prebuilt-source.json", "LICENSE", "NOTICE.md"):
            shutil.copy2(package.ROOT / name, self.root / name)
        self.source = json.loads((self.root / "prebuilt-source.json").read_text())
        (self.root / "release_assets").mkdir()
        shutil.copy2(package.ROOT / "release_assets" / self.source["file"],
                     self.root / "release_assets" / self.source["file"])
        environment = {key: value for key, value in os.environ.items() if not key.startswith("FAST_DEPLOY_")}
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def extract(self, artifact, name):
        output = self.base / name
        output.mkdir()
        with tarfile.open(artifact, "r:gz") as archive:
            archive.extractall(output, filter="data")
        return output / package.PREFIX

    def guard_fixture(self, name="guard", number=10, version="1.0.0"):
        """仅为目标状态机生成测试身份；正式生产者测试另走真实 Core。"""
        artifact, _ = package.build(self.root)
        root = self.extract(artifact, name)
        build = json.loads((root / "build.json").read_text())
        build.update(local_validation=False, build_number=number, version=version, run_id=name,
                     source_commit_sha="a" * 40, input_sha256="b" * 64)
        (root / "build.json").write_bytes(package.encoded(build))
        (root / "VERSION").write_text(version + "\n")
        self.refresh_sums(root)
        return root

    @staticmethod
    def refresh_sums(root):
        files = sorted(path for path in root.rglob("*") if path.is_file() and path.name != "SHA256SUMS")
        (root / "SHA256SUMS").write_text("".join(hashlib.sha256(path.read_bytes()).hexdigest() + "  " +
                                                path.relative_to(root).as_posix() + "\n" for path in files))

    @staticmethod
    def install_fixture(root, system):
        """复制到临时目录模拟文件落盘，不调用真实安装器或设备命令。"""
        for path in root.rglob("*"):
            if path.is_file() and (name := verification.destination(path.relative_to(root).as_posix())):
                target = system / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)


class PackageTest(PackageFixture):
    def test_run_output_rejects_local_mode_and_linked_parent(self):
        with self.assertRaisesRegex(ValueError, "完整正式输入"):
            package.build(self.root, self.base / "run.tar.gz")
        linked = self.base / "linked"
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            package.build(self.root, linked / "run.tar.gz")
        self.assertFalse((self.root / "dist").exists())

    def test_real_native_bytes_deterministic_local_package_and_install_rejection(self):
        artifact, report = package.build(self.root)
        self.assertTrue(report["build"]["local_validation"])
        self.assertNotIn("build_number", report["build"])
        self.assertEqual(package.build(self.root)[1]["artifact_sha256"], report["artifact_sha256"])
        root = self.extract(artifact, "local")
        payload, native = package.source_payload(self.root)
        for name, data in payload.items():
            self.assertEqual((root / name).read_bytes(), data)
        self.assertEqual(native["modules"]["aic8800_fdrv.ko.zst"]["version"], "6.4.3.0")
        self.assertEqual(native["modules"]["aic_load_fw.ko.zst"]["version"], "1.0")
        self.assertEqual(native["source"]["sha256"], self.source["sha256"])
        shim = self.base / "bin"
        shim.mkdir()
        marker = self.base / "privileged-called"
        for name in ("id", "uname", "install", "cp", "rm", "modprobe", "depmod", "flock", "date", "mokutil"):
            command = shim / name
            command.write_text('#!/bin/sh\nprintf called > "$UGREEN_TEST_MARKER"\nexit 90\n')
            command.chmod(0o755)
        result = subprocess.run(["bash", str(root / "scripts/install_prebuilt.sh")],
                                env={**os.environ, "PATH": str(shim) + ":" + os.environ["PATH"],
                                     "UGREEN_TEST_MARKER": str(marker)}, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("本地验证包", result.stderr)
        self.assertFalse(marker.exists())

    def test_source_digest_native_architecture_and_kernel_must_match(self):
        source = self.root / "release_assets" / self.source["file"]
        original = source.read_bytes()
        source.write_bytes(original + b"changed")
        with self.assertRaisesRegex(ValueError, "SHA256"):
            package.build(self.root)
        source.write_bytes(original)
        payload, _ = package.source_payload(self.root)
        compressed = next(value for name, value in payload.items() if name.endswith(".ko.zst"))
        elf = subprocess.check_output(["zstd", "-q", "-d", "-c"], input=compressed)
        for modified in (elf[:18] + b"\xb7\x00" + elf[20:],
                         elf.replace(b"6.17.0-14-generic", b"6.17.0-15-generic")):
            candidate = subprocess.check_output(["zstd", "-q", "-c"], input=modified)
            with self.assertRaises(ValueError):
                package.native_module(candidate)
        self.assertFalse((self.root / "dist").exists())

    def test_invalid_version_and_incomplete_formal_input_fail(self):
        declaration = self.root / "release.json"
        original = declaration.read_bytes()
        for value in ('{"version":"01.0.0"}', '{"version":true}', '{"version":"1.0.0","version":"1.0.1"}'):
            declaration.write_text(value)
            with self.assertRaises(ValueError):
                package.build(self.root)
        declaration.write_bytes(original)
        os.environ["FAST_DEPLOY_RUN_ID"] = "incomplete"
        with self.assertRaisesRegex(ValueError, "冻结的"):
            package.build(self.root)
        self.assertFalse((self.root / "dist").exists())

    def test_extracted_package_tampering_missing_and_linked_files_fail(self):
        root = self.guard_fixture()
        verification.verify(root)
        path = root / "udev/aic.rules"
        original = path.read_bytes()
        path.write_bytes(original + b"# changed\n")
        with self.assertRaisesRegex(ValueError, "摘要不一致"):
            verification.verify(root)
        path.unlink()
        with self.assertRaises(ValueError):
            verification.verify(root)
        external = self.base / "external"
        external.write_bytes(original)
        path.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            verification.verify(root)
        path.unlink()
        path.write_bytes(original)
        sums = root / "SHA256SUMS"
        sums.write_bytes(sums.read_bytes() + sums.read_bytes().splitlines()[0] + b"\n")
        with self.assertRaisesRegex(ValueError, "重复"):
            verification.verify(root)

    def test_install_pending_recovery_upgrade_stale_and_changed_identity(self):
        root = self.guard_fixture()
        newer = self.guard_fixture("newer", 11)
        state, system = self.base / "state/install.json", self.base / "system"
        system.mkdir()
        token = verification.begin_install(root, state, system)
        self.assertEqual(verification.read_state(state)["status"], "pending")
        with self.assertRaisesRegex(ValueError, "未完成"):
            verification.begin_install(newer, state, system)
        with self.assertRaises(ValueError):
            verification.finish_install(root, state, system, token)
        self.assertEqual(verification.begin_install(root, state, system), token)
        self.install_fixture(root, system)
        verification.finish_install(root, state, system, token)
        token = verification.begin_install(newer, state, system)
        self.install_fixture(newer, system)
        verification.finish_install(newer, state, system, token)
        with self.assertRaisesRegex(ValueError, "较旧"):
            verification.begin_install(root, state, system)
        same_number = self.guard_fixture("same-number", 11)
        with self.assertRaisesRegex(ValueError, "同号"):
            verification.begin_install(same_number, state, system)
        old_version = self.guard_fixture("old-version", 12, "0.9.0")
        with self.assertRaisesRegex(ValueError, "较旧"):
            verification.begin_install(old_version, state, system)
        self.assertEqual(verification.read_state(state)["build"]["build_number"], 11)
        installed = next(iter(verification.read_state(state)["files"]))
        (system / installed).write_bytes(b"outside change")
        with self.assertRaisesRegex(ValueError, "实际文件"):
            verification.begin_install(newer, state, system)

    def test_legacy_state_and_merged_usr_alias(self):
        root = self.guard_fixture()
        system = self.base / "system"
        (system / "usr/lib").mkdir(parents=True)
        (system / "lib").symlink_to("usr/lib")
        state = self.base / "state/install.json"
        legacy = system / "lib/modules" / package.KERNEL / "updates/legacy/aic8800_fdrv.ko"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"old")
        with self.assertRaisesRegex(ValueError, "没有安装记录"):
            verification.begin_install(root, state, system)
        self.assertFalse(state.exists())
        legacy.unlink()
        token = verification.begin_install(root, state, system)
        self.install_fixture(root, system)
        verification.finish_install(root, state, system, token)
        self.assertEqual(verification.read_state(state)["status"], "installed")


@unittest.skipUnless(os.environ.get("FAST_DEPLOY_TEST_PACKAGE_CORE_ROOT"), "独立 run 测试需要显式 Core 测试源码")
class PackageRunTest(PackageFixture):
    def setUp(self):
        core_source = Path(os.environ["FAST_DEPLOY_TEST_PACKAGE_CORE_ROOT"])
        super().setUp()
        self.core_repo = self.base / "core-repo"
        self.core = self.core_repo / "core"
        shutil.copytree(core_source / "shared", self.core / "shared",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (self.core_repo / ".gitignore").write_text("__pycache__/\n")
        self.remotes = {}
        for name, root in (("module", self.root), ("core", self.core_repo)):
            subprocess.run(["git", "init", "-qb", "master", str(root)], check=True)
            sha = commit(root)
            subprocess.run(["git", "-C", str(root), "branch", "1.0.0"], check=True)
            remote = self.base / (name + ".git")
            subprocess.run(["git", "clone", "-q", "--bare", str(root), str(remote)], check=True)
            subprocess.run(["git", "-C", str(root), "remote", "add", "origin", str(remote)], check=True)
            subprocess.run(["git", "-C", str(root), "fetch", "-q", "origin"], check=True)
            self.remotes[name] = remote
            if name == "core":
                self.core_sha = sha
        self.state_root = self.base / "artifacts"
        self.state_root.mkdir(mode=0o700)
        self.scope = {"repo_name": "ugreen-ax900", "project_path": "", "platform": "module",
                      "identity_kind": "release_unit", "application_id": "ugreen-ax900"}
        scope_file = self.base / "scope.json"
        scope_file.write_text(json.dumps(self.scope))
        subprocess.run([sys.executable, str(self.core / "shared/client_version.py"), "initialize",
                        "--artifact-root", str(self.state_root), "--scope-file", str(scope_file),
                        "--historical-code", "1", "--approved-core-sha", self.core_sha], check=True,
                       capture_output=True)
        os.environ.update(FAST_DEPLOY_TEST_ALLOW_LOCAL_REPOSITORY_URLS="true",
                          FAST_DEPLOY_TEST_CORE_REPO_URL=str(self.remotes["core"]))

    def invoke(self, run):
        result = subprocess.run([sys.executable, str(self.core / "shared/package_run.py"),
            "--repo-root", str(self.root), "--repo-url", str(self.remotes["module"]),
            "--manifest", "scripts/module-package-run.json", "--source-ref", "refs/heads/1.0.0",
            "--artifact-root", str(self.state_root), "--receipt-root", str(self.base / "receipts"),
            "--run-id", run], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_real_entry_two_runs_and_replay(self):
        first = self.invoke("package-first")
        second = self.invoke("package-second")
        self.assertEqual((first["build"]["version"], first["build"]["build_number"]), ("1.0.0", 2))
        self.assertEqual((second["build"]["version"], second["build"]["build_number"]), ("1.0.0", 3))
        self.assertEqual(self.invoke("package-first"), first)
        for run, number in (("package-first", 2), ("package-second", 3)):
            target = self.base / "receipts" / run / "ugreen-ax900.tar.gz"
            original = self.root / "dist" / f"{package.PREFIX}-{package.VARIANT}-{package.ARCHITECTURE}-1.0.0-build{number}.tar.gz"
            self.assertEqual(target.read_bytes(), original.read_bytes())
            extracted = self.extract(target, run)
            build = verification.verify(extracted)["build"]
            self.assertEqual((build["version"], build["build_number"]), ("1.0.0", number))
            self.assertEqual(first["context"]["artifact_kind"], "dist-tar-gz")


@unittest.skipUnless(CORE_SOURCE, "正式构建测试需要显式 Core 测试源码")
class FormalPackageTest(PackageFixture):
    def setUp(self):
        super().setUp()
        self.core = self.base / "core"
        shutil.copytree(Path(CORE_SOURCE) / "shared", self.core / "shared",
                        ignore=shutil.ignore_patterns("__pycache__"))
        for root in (self.root, self.core):
            git(root, "init", "-q")
            commit(root)
        self.core_sha = git(self.core, "rev-parse", "HEAD")
        self.state_root = self.base / "allocator"
        self.state_root.mkdir(mode=0o700)
        sys.path.insert(0, str(self.core / "shared"))
        import client_version
        import release_version_state
        self.version, self.state = client_version, release_version_state
        os.environ["FAST_DEPLOY_PACKAGE_CORE_ROOT"] = str(self.core)

    def allocate(self, run, version="1.0.0", variant=package.VARIANT):
        declaration = self.root / "release.json"
        if json.loads(declaration.read_text())["version"] != version:
            declaration.write_bytes(package.encoded({"version": version}))
            commit(self.root)
        sha = git(self.root, "rev-parse", "HEAD")
        git(self.root, "update-ref", "refs/remotes/origin/" + version, sha)
        context = dict(source_ref="refs/heads/" + version, run_id=run, repo_name="ugreen-ax900",
                       resolved_commit_sha=sha, core_commit_sha=self.core_sha, job_manifest_sha256="b" * 64,
                       profile_snapshot_id="c" * 64, config_path="release.json", build_env="",
                       build_type="release", build_channel="", artifact_kind="module")
        os.environ.update({self.version.CONTEXT_ENV_FIELDS[key]: value for key, value in context.items()})
        scope = self.version.scope_from_context(context, "module", package.PACKAGE_ID)
        if not (self.state_root / "release-versions" / self.state.scope_key(scope)).exists():
            self.state.initialize(self.state_root, scope, 71, self.core_sha)
        result = self.state.allocate(self.state_root, self.root, "module", package.PACKAGE_ID,
                                     variant, [package.ARCHITECTURE], dict(os.environ), self.core)
        snapshot = self.base / "snapshot.json"
        snapshot.write_bytes(package.encoded({"protocol_version": 1, "ok": True,
                                              "version_snapshot": result["version_snapshot"]}))
        os.environ["FAST_DEPLOY_CLIENT_VERSION_SNAPSHOT_FILE"] = str(snapshot)

    def test_actual_tar_two_builds_replay_version_change_and_installer_readback(self):
        hashes, evidence = {}, []
        for index, (run, version, number) in enumerate((("first", "1.0.0", 72), ("first", "1.0.0", 72),
                                                       ("second", "1.0.0", 73), ("next", "1.0.1", 74))):
            self.allocate(run, version)
            artifact, report = package.build(self.root)
            self.assertEqual((report["build"]["version"], report["build"]["build_number"]), (version, number))
            extracted = self.extract(artifact, "formal-" + str(index))
            self.assertEqual(verification.verify(extracted)["build"], report["build"])
            if run in hashes:
                self.assertEqual(hashes[run], report["artifact_sha256"])
            hashes[run] = report["artifact_sha256"]
            evidence.append(report)
        self.assertEqual(len(list((self.root / "dist").glob("*.tar.gz"))), 3)
        if EVIDENCE_FILE:
            Path(EVIDENCE_FILE).write_bytes(package.encoded({"source": self.source, "builds": evidence}))

    def test_dirty_source_core_wrong_variant_and_existing_artifact_refuse(self):
        self.allocate("wrong-target", variant="another-kernel")
        with self.assertRaises(subprocess.CalledProcessError):
            package.build(self.root)
        self.allocate("first")
        core = self.core / "shared/package_identity.py"
        original = core.read_bytes()
        core.write_bytes(original + b"\n# dirty\n")
        with self.assertRaises(subprocess.CalledProcessError):
            package.build(self.root)
        core.write_bytes(original)
        source = self.root / "scripts/install_prebuilt.sh"
        original = source.read_bytes()
        source.write_bytes(original + b"\n# dirty\n")
        with self.assertRaises(subprocess.CalledProcessError):
            package.build(self.root)
        source.write_bytes(original)
        artifact, _ = package.build(self.root)
        original = artifact.read_bytes()
        artifact.write_bytes(b"historical different bytes")
        with self.assertRaisesRegex(ValueError, "不同制品"):
            package.build(self.root)
        self.assertEqual(artifact.read_bytes(), b"historical different bytes")
        artifact.write_bytes(original)
        receipt = artifact.with_name(artifact.name + ".json")
        receipt.write_bytes(b"historical different receipt")
        with self.assertRaisesRegex(ValueError, "不同收据"):
            package.build(self.root)
        self.assertEqual(receipt.read_bytes(), b"historical different receipt")

    def test_formal_run_output_preserves_original_and_rejects_conflict(self):
        self.allocate("run-output")
        target = self.base / "output.tar.gz"
        artifact, report = package.build(self.root, target)
        self.assertEqual(artifact, target)
        original = next((self.root / "dist").glob("*.tar.gz"))
        self.assertEqual(target.read_bytes(), original.read_bytes())
        self.assertEqual(package.build(self.root, target)[1], report)
        target.write_bytes(b"different existing archive")
        with self.assertRaisesRegex(ValueError, "已有不同制品"):
            package.build(self.root, target)
        self.assertEqual(target.read_bytes(), b"different existing archive")


if __name__ == "__main__":
    unittest.main()
