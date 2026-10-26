#!/usr/bin/env python3
"""校验驱动分发包与目标安装记录；不加载模块或调用设备。"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile

KERNEL = "7.0.0-38-generic"
ARCHITECTURE = "x86_64"
MODULE_NAMES = {"aic8800_fdrv.ko.zst", "aic_load_fw.ko.zst"}
VARIANT = "ubuntu24.04-kernel" + KERNEL
SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", re.ASCII)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()


def safe_path(path):
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("校验路径不能经过符号链接：" + str(path))
    return path


def read_file(path):
    safe_path(path)
    if not path.is_file():
        raise ValueError("缺少普通文件：" + str(path))
    return path.read_bytes()


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("JSON 不允许重复字段")
        value[key] = item
    return value


def read_json(path):
    return json.loads(read_file(path), object_pairs_hook=unique_object)


def validate_build(build, historical=False):
    fields = {"version", "platform", "application_id", "architecture", "variant", "local_validation",
              "run_id", "build_number", "source_commit_sha", "input_sha256"}
    if type(build) is not dict or build.get("local_validation") is not False:
        raise ValueError("只允许正式冻结包安装，本地验证包不能安装")
    if (set(build) != fields or not isinstance(build["version"], str)
            or not VERSION.fullmatch(build["version"]) or build["platform"] != "module"
            or build["application_id"] != "ugreen-ax900" or build["architecture"] != ARCHITECTURE
            or build["variant"] not in ({VARIANT, "ubuntu24.04-kernel6.17.0-14-generic"} if historical else {VARIANT})
            or type(build["build_number"]) is not int
            or not 0 < build["build_number"] <= 9007199254740991
            or not isinstance(build["run_id"], str) or not build["run_id"].strip()
            or not isinstance(build["source_commit_sha"], str)
            or not re.fullmatch(r"[0-9a-f]{40}", build["source_commit_sha"])
            or not isinstance(build["input_sha256"], str) or not SHA256.fullmatch(build["input_sha256"])):
        raise ValueError("驱动包正式身份不完整或目标不一致")


def destination(name):
    module_prefix = f"modules/{KERNEL}/{ARCHITECTURE}/"
    if name.startswith(module_prefix) and name[len(module_prefix):] in MODULE_NAMES:
        return f"lib/modules/{KERNEL}/updates/dkms/" + Path(name).name
    if name.startswith("firmware/aic8800D80/"):
        return "lib/" + name
    return {"udev/aic.rules": "usr/lib/udev/rules.d/aic.rules",
            "modules-load/aic8800.conf": "etc/modules-load.d/aic8800.conf"}.get(name)


def verify(root):
    safe_path(root)
    build = read_json(root / "build.json")
    validate_build(build)
    if read_file(root / "VERSION") != (build["version"] + "\n").encode():
        raise ValueError("包内 VERSION 与正式身份不一致")
    sums = read_file(root / "SHA256SUMS")
    expected = {}
    for line in sums.decode("ascii").splitlines():
        if len(line) < 67 or line[64:66] != "  " or not SHA256.fullmatch(line[:64]):
            raise ValueError("SHA256SUMS 格式非法")
        name = line[66:]
        path = PurePosixPath(name)
        if (path.is_absolute() or ".." in path.parts or "\\" in name
                or str(path) != name or name in expected or name == "SHA256SUMS"):
            raise ValueError("SHA256SUMS 含重复或越界路径")
        expected[name] = line[:64]
    required = {"VERSION", "build.json", "native.json", "LICENSE", "NOTICE.md", "README.release.md",
                "scripts/install_prebuilt.sh", "scripts/collect_diagnostics.sh", "scripts/verify_package.py",
                "udev/aic.rules", "modules-load/aic8800.conf",
                *("modules/" + KERNEL + "/" + ARCHITECTURE + "/" + name for name in MODULE_NAMES)}
    if not required <= expected.keys() or any(name not in required and
                                               not name.startswith("firmware/aic8800D80/") for name in expected):
        raise ValueError("包文件集合缺少必要入口或包含未声明用途文件")
    actual = set()
    for path in root.rglob("*"):
        safe_path(path)
        if path.is_dir():
            continue
        name = path.relative_to(root).as_posix()
        if not path.is_file():
            raise ValueError("包只能包含普通文件与目录")
        actual.add(name)
        if name != "SHA256SUMS" and (name not in expected or digest(read_file(path)) != expected[name]):
            raise ValueError("包内文件摘要不一致：" + name)
    if actual != expected.keys() | {"SHA256SUMS"}:
        raise ValueError("包内文件集合与 SHA256SUMS 不一致")
    native = read_json(root / "native.json")
    if (set(native) != {"source", "kernel", "architecture", "modules", "compile_provenance"}
            or native["kernel"] != KERNEL or native["architecture"] != ARCHITECTURE
            or set(native["modules"]) != MODULE_NAMES):
        raise ValueError("原生模块目标与包身份不一致")
    for name, facts in native["modules"].items():
        if (facts["compressed_sha256"] != expected[f"modules/{KERNEL}/{ARCHITECTURE}/{name}"]
                or facts["architecture"] != ARCHITECTURE or not facts["vermagic"].startswith(KERNEL + " ")):
            raise ValueError("实际模块摘要或内核与原生记录不同")
    provenance = native["compile_provenance"]
    if (provenance["schema_version"] != 1 or provenance["kernel"] != KERNEL
            or provenance["architecture"] != ARCHITECTURE or provenance["modules"] != native["modules"]
            or digest(encoded(provenance["inputs"])) != provenance["inputs_sha256"]
            or provenance["payload_sha256"] != {name: sha for name, sha in expected.items() if destination(name)}):
        raise ValueError("源码编译来源与实际安装载荷不同")
    firmware_inputs = {name: sha for name, sha in provenance["inputs"].items() if name.startswith("firmware/")}
    if firmware_inputs != {name: sha for name, sha in expected.items() if name.startswith("firmware/")}:
        raise ValueError("固件与冻结源码来源不同")
    installed = {destination(name): sha for name, sha in expected.items() if destination(name)}
    if not any(name.startswith("lib/firmware/aic8800D80/") for name in installed):
        raise ValueError("驱动包缺少固件")
    return {"schema_version": 1, "build": build, "payload_sha256": digest(sums), "files": installed}


def read_state(state):
    safe_path(state)
    if not state.exists():
        return None
    value = read_json(state)
    if (type(value) is not dict or set(value) != {"schema_version", "build", "payload_sha256", "files", "status"}
            or value["schema_version"] != 1 or value["status"] not in {"pending", "installed"}
            or not isinstance(value["payload_sha256"], str) or not SHA256.fullmatch(value["payload_sha256"])
            or type(value["files"]) is not dict or not value["files"]):
        raise ValueError("安装记录格式不完整，需先核定历史状态")
    # 旧内核的已安装状态仍参与构建号高水位、降级与漂移检查；候选包仅接受当前目标。
    validate_build(value["build"], historical=True)
    for name, sha in value["files"].items():
        path = PurePosixPath(name)
        if (path.is_absolute() or ".." in path.parts or "\\" in name or str(path) != name
                or not isinstance(sha, str) or not SHA256.fullmatch(sha)):
            raise ValueError("安装记录文件非法")
    return value


def installed_matches(record, system_root):
    for name, sha in record["files"].items():
        if digest(read_file(system_path(system_root, name))) != sha:
            raise ValueError("目标实际文件与安装记录不一致：" + name)


def system_path(system_root, name):
    # Ubuntu merged-/usr 的 /lib 合法指向 /usr/lib；只允许包内记录仍落在目标根内。
    root = system_root.resolve()
    path = root / name
    resolved = path.parent.resolve() / path.name
    if not resolved.is_relative_to(root) or path.is_symlink():
        raise ValueError("安装目标路径越界或文件为符号链接")
    return resolved


def save_state(state, value):
    safe_path(state)
    state.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".install-", dir=state.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state)
        directory = os.open(state.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def begin_install(root, state, system_root):
    """调用方持有目标 flock；先保留待安装身份，失败只能重试同包。"""
    candidate = verify(root)
    previous = read_state(state)
    if previous:
        old = {key: value for key, value in previous.items() if key != "status"}
        if previous["status"] == "pending":
            if old != candidate:
                raise ValueError("存在未完成安装，必须先用原包恢复")
        else:
            before, after = previous["build"], candidate["build"]
            if (after["build_number"] < before["build_number"]
                    or tuple(map(int, after["version"].split("."))) < tuple(map(int, before["version"].split(".")))):
                raise ValueError("拒绝较旧版本或构建号覆盖已安装包")
            if after["build_number"] == before["build_number"] and old != candidate:
                raise ValueError("同号身份或内容不同，拒绝覆盖")
            installed_matches(previous, system_root)
    else:
        modules = system_path(system_root, "lib/modules/" + KERNEL)
        safe_path(modules)
        if any(path.name.split(".ko", 1)[0] in {"aic8800_fdrv", "aic_load_fw"}
               for path in modules.rglob("*") if ".ko" in path.name):
            raise ValueError("已有驱动但没有安装记录，需先核定历史版本与构建高水位")
    pending = {**candidate, "status": "pending"}
    save_state(state, pending)
    return digest(encoded(pending))


def finish_install(root, state, system_root, token):
    candidate = verify(root)
    pending = {**candidate, "status": "pending"}
    if read_state(state) != pending or digest(encoded(pending)) != token:
        raise ValueError("安装期间包或目标记录发生变化")
    installed_matches(candidate, system_root)
    save_state(state, {**candidate, "status": "installed"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "begin-install", "finish-install"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--state", type=Path, default=Path("/var/lib/ugreen-ax900/install.json"))
    parser.add_argument("--token")
    args = parser.parse_args()
    try:
        # 解析根目录仅用于消除调用者的 ./ 与 /tmp 别名；包内路径仍逐项拒绝链接。
        root = args.root.resolve(strict=True)
        if args.action == "verify":
            print(json.dumps(verify(root), sort_keys=True))
        elif args.action == "begin-install":
            print(begin_install(root, args.state, Path("/")))
        else:
            finish_install(root, args.state, Path("/"), args.token)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise SystemExit("verify_package result\n  status       failed\n  reason       " + str(error)) from error
