#!/usr/bin/env python3
"""从固定历史原件封装驱动分发包；不编译、安装或加载内核模块。"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ID = "ugreen-ax900"
PREFIX = "ugreen-ax900-prebuilt"
KERNEL = "6.17.0-14-generic"
ARCHITECTURE = "x86_64"
VARIANT = "ubuntu24.04-kernel" + KERNEL


def encoded(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("JSON 不允许重复字段")
        value[key] = item
    return value


def read_file(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("构建输入必须是普通文件：" + str(path))
    return path.read_bytes()


def identity(root):
    declaration = json.loads(read_file(root / "release.json"), object_pairs_hook=unique_object)
    if (type(declaration) is not dict or set(declaration) != {"version"}
            or type(declaration["version"]) is not str
            or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
                                declaration["version"], re.ASCII)):
        raise ValueError("release.json 必须声明严格三段版本")
    if (root / "VERSION").exists() or (root / "VERSION").is_symlink():
        raise ValueError("源码不能保留平行 VERSION，包内 VERSION 由本次输入生成")
    formal = bool(os.environ.get("FAST_DEPLOY_RUN_ID") or
                  os.environ.get("FAST_DEPLOY_CLIENT_VERSION_SNAPSHOT_FILE"))
    if not formal:
        return {"version": declaration["version"], "platform": "module", "application_id": PACKAGE_ID,
                "architecture": ARCHITECTURE, "variant": VARIANT,
                "local_validation": True, "run_id": ""}, None
    core = os.environ.get("FAST_DEPLOY_PACKAGE_CORE_ROOT", "")
    if not core or not Path(core).is_absolute():
        raise ValueError("正式驱动包必须显式提供冻结的 FAST_DEPLOY_PACKAGE_CORE_ROOT")
    command = [sys.executable, str(Path(core) / "shared/package_identity.py"), "resolve",
               "--project-root", str(root), "--platform", "module", "--application-id", PACKAGE_ID,
               "--architecture", ARCHITECTURE, "--variant", VARIANT]
    value = json.loads(subprocess.check_output(command))
    if value["local_validation"] is not False:
        raise ValueError("正式入口不能回落为本地包")
    return value, command


def native_module(compressed):
    data = subprocess.check_output(["zstd", "-q", "-d", "-c"], input=compressed)
    if len(data) < 64 or data[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<HH", data, 16) != (1, 62):
        raise ValueError("预编译模块必须是真实 x86_64 ELF 可重定位对象")
    offset = struct.unpack_from("<Q", data, 40)[0]
    size, count, names_index = struct.unpack_from("<HHH", data, 58)
    if size != 64 or not 0 < names_index < count or offset + count * size > len(data):
        raise ValueError("ELF section 表越界")
    sections = [struct.unpack_from("<IIQQQQIIQQ", data, offset + i * size) for i in range(count)]
    names = sections[names_index]
    if names[4] + names[5] > len(data):
        raise ValueError("ELF 字符串表越界")
    string_table = data[names[4]:names[4] + names[5]]
    records = []
    for section in sections:
        if section[0] >= len(string_table):
            raise ValueError("ELF section 名越界")
        name = string_table[section[0]:].split(b"\0", 1)[0]
        if name == b".modinfo":
            if section[4] + section[5] > len(data):
                raise ValueError("ELF modinfo 越界")
            records.extend(data[section[4]:section[4] + section[5]].split(b"\0"))
    facts = {}
    for key in ("name", "version", "srcversion", "vermagic"):
        values = [item[len(key) + 1:].decode("ascii") for item in records if item.startswith(key.encode() + b"=")]
        if len(values) != 1:
            raise ValueError("内核模块字段缺失或不唯一：" + key)
        facts[key] = values[0]
    if not facts["vermagic"].startswith(KERNEL + " "):
        raise ValueError("实际内核模块 vermagic 与目标不一致")
    return {**facts, "architecture": ARCHITECTURE, "elf_sha256": hashlib.sha256(data).hexdigest(),
            "elf_bytes": len(data), "compressed_sha256": hashlib.sha256(compressed).hexdigest()}


def source_payload(root):
    source = json.loads(read_file(root / "prebuilt-source.json"), object_pairs_hook=unique_object)
    if (set(source) != {"file", "url", "sha256", "prefix", "kernel", "architecture"}
            or source["kernel"] != KERNEL or source["architecture"] != ARCHITECTURE
            or Path(source["file"]).name != source["file"]
            or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
            or not re.fullmatch(r"[a-zA-Z0-9_-]+", source["prefix"])):
        raise ValueError("预编译来源声明不匹配实际交付目标")
    if (root / "release_assets").is_symlink():
        raise ValueError("预编译输入目录不能是符号链接")
    data = read_file(root / "release_assets" / source["file"])
    if hashlib.sha256(data).hexdigest() != source["sha256"]:
        raise ValueError("预编译原件 SHA256 不匹配冻结来源")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        names = set()
        for item in archive.getmembers():
            path = PurePosixPath(item.name)
            if (path.is_absolute() or ".." in path.parts or "\\" in item.name
                    or item.name in names or item.name.rstrip("/") != str(path)
                    or not (item.isfile() or item.isdir())):
                raise ValueError("预编译原件包含不允许的归档条目")
            names.add(item.name)
            if not item.isfile() or path.parts[0] != source["prefix"] or any(p.startswith("._") for p in path.parts):
                continue
            relative = "/".join(path.parts[1:])
            if relative.startswith(("modules/", "firmware/")) or relative in ("udev/aic.rules", "modules-load/aic8800.conf"):
                files[relative] = archive.extractfile(item).read()
    module_prefix = f"modules/{KERNEL}/{ARCHITECTURE}/"
    expected_modules = {module_prefix + name + ".ko.zst" for name in ("aic_load_fw", "aic8800_fdrv")}
    if {name for name in files if name.startswith("modules/")} != expected_modules:
        raise ValueError("预编译原件的模块集合不匹配")
    modules = {}
    for name in sorted(expected_modules):
        fact = native_module(files[name])
        if name != module_prefix + fact["name"] + ".ko.zst":
            raise ValueError("实际内核模块名与文件名不一致")
        modules[Path(name).name] = fact
    if not any(name.startswith("firmware/aic8800D80/") for name in files):
        raise ValueError("预编译原件缺少厂商固件")
    for name in ("udev/aic.rules", "modules-load/aic8800.conf"):
        if name not in files:
            raise ValueError("预编译原件缺少安装配置")
    return files, {"source": source, "kernel": KERNEL, "architecture": ARCHITECTURE, "modules": modules}


def verify(path, value, files):
    with tarfile.open(path, "r:gz") as archive:
        entries = archive.getmembers()
        if sorted(item.name for item in entries) != sorted(PREFIX + "/" + name for name in files):
            raise ValueError("最终 tar 文件集合不同")
        for item in entries:
            name = item.name[len(PREFIX) + 1:]
            data, mode = files[name]
            if not item.isfile() or item.mode != mode or archive.extractfile(item).read() != data:
                raise ValueError("最终 tar 字节或权限不一致")
    return {"schema_version": 1, "build": value,
            "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "artifact_bytes": path.stat().st_size}


def build(root, artifact=None):
    if artifact is not None:
        artifact = Path(artifact)
        if (not artifact.is_absolute() or not artifact.name.endswith(".tar.gz")
                or artifact.parent.resolve() != artifact.parent or not artifact.parent.is_dir()
                or artifact.is_symlink()):
            raise ValueError("正式 run 的输出必须是已有目录下的绝对 tar.gz 路径，且不得经过符号链接")
        if not os.environ.get("FAST_DEPLOY_RUN_ID") or not os.environ.get("FAST_DEPLOY_CLIENT_VERSION_SNAPSHOT_FILE"):
            raise ValueError("独立 run 输出只接受完整正式输入")
    value, command = identity(root)
    payload, native = source_payload(root)
    files = {name: (data, 0o644) for name, data in payload.items()}
    for name in ("scripts/install_prebuilt.sh", "scripts/collect_diagnostics.sh", "scripts/verify_package.py"):
        files[name] = (read_file(root / name), 0o755)
    for name in ("LICENSE", "NOTICE.md"):
        files[name] = (read_file(root / name), 0o644)
    files["VERSION"] = ((value["version"] + "\n").encode(), 0o644)
    files["build.json"] = (encoded(value), 0o644)
    files["native.json"] = (encoded(native), 0o644)
    files["README.release.md"] = (
        ("# UGREEN AX900 驱动分发包\n\n目标：Ubuntu 24.04 / " + KERNEL + " / " + ARCHITECTURE +
         "\n\n正式包先执行 `python3 scripts/verify_package.py verify .`，再由 root 执行 "
         "`bash scripts/install_prebuilt.sh`。本地验证包拒绝安装。安装后按 README 的硬件边界物理拔插并运行 "
         "`bash scripts/collect_diagnostics.sh`。本包保留厂商模块版本，产品身份见 build.json。\n").encode(), 0o644)
    checksums = "".join(hashlib.sha256(data).hexdigest() + "  " + name + "\n"
                        for name, (data, _) in sorted(files.items()))
    files["SHA256SUMS"] = (checksums.encode(), 0o644)
    output = root / "dist"
    if output.is_symlink():
        raise ValueError("dist 不能是符号链接")
    output.mkdir(exist_ok=True)
    label = "local" if value["local_validation"] else "build" + str(value["build_number"])
    path = output / f"{PREFIX}-{VARIANT}-{ARCHITECTURE}-{value['version']}-{label}.tar.gz"
    receipt = output / (path.name + ".json")
    with tempfile.TemporaryDirectory(prefix=".package-", dir=output) as temporary:
        candidate = Path(temporary) / "package.tar.gz"
        with candidate.open("wb") as stream, gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name, (data, mode) in sorted(files.items()):
                    item = tarfile.TarInfo(PREFIX + "/" + name)
                    item.size, item.mode, item.mtime = len(data), mode, 0
                    archive.addfile(item, io.BytesIO(data))
        if identity(root)[0] != value:
            raise ValueError("打包期间冻结身份发生变化")
        report = verify(candidate, value, files)
        if command:
            command[2] = "verify-tar"
            actual = json.loads(subprocess.check_output([*command, "--artifact", str(candidate),
                                                        "--identity-entry", PREFIX + "/build.json"]))
            if actual != report:
                raise ValueError("Core 最终 tar 回读不一致")
        if path.is_symlink() or receipt.is_symlink():
            raise ValueError("制品与收据不能是符号链接")
        if not value["local_validation"]:
            if receipt.exists() and receipt.read_bytes() != encoded(report):
                raise ValueError("同号已有不同收据，拒绝覆盖")
            try:
                os.link(candidate, path)
            except FileExistsError:
                if path.is_symlink() or path.read_bytes() != candidate.read_bytes():
                    raise ValueError("同号已有不同制品，拒绝覆盖")
        else:
            candidate.replace(path)
        receipt_candidate = Path(temporary) / "receipt.json"
        receipt_candidate.write_bytes(encoded(report))
        if value["local_validation"]:
            receipt_candidate.replace(receipt)
        else:
            try:
                os.link(receipt_candidate, receipt)
            except FileExistsError:
                if receipt.is_symlink() or receipt.read_bytes() != encoded(report):
                    raise ValueError("同号已有不同收据，拒绝覆盖")
    if artifact is not None:
        with tempfile.TemporaryDirectory(prefix=".driver-run-", dir=artifact.parent) as temporary:
            candidate = Path(temporary) / "package.tar.gz"
            shutil.copyfile(path, candidate)
            if verify(candidate, value, files) != report:
                raise ValueError("独立 run 包复制后回读不同")
            try:
                os.link(candidate, artifact)
            except FileExistsError:
                if artifact.is_symlink() or artifact.read_bytes() != candidate.read_bytes():
                    raise ValueError("独立 run 路径已有不同制品，拒绝覆盖")
        path = artifact
    return path, report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", nargs="?", type=Path, help="Core 独立 run 的最终 tar.gz 绝对路径")
    args = parser.parse_args()
    try:
        path, report = build(ROOT, args.artifact)
        print("package_release result\n  status       ok\n  artifact     " + str(path) +
              "\n  sha256       " + report["artifact_sha256"])
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError, tarfile.TarError) as error:
        raise SystemExit("package_release result\n  status       failed\n  reason       " + str(error)) from error
