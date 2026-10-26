#!/usr/bin/env python3
"""在 Ubuntu 临时源码副本中无特权编译固定内核模块，输出待冻结的原生输入归档。"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

import build_package as package

def output(command):
    return subprocess.check_output(command, text=True, env={**os.environ, "LC_ALL": "C"}).strip()


def tool_fact(command):
    path = Path(shutil.which(command) or command).resolve(strict=True)
    return {"version": output([str(path), "--version"]),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def header_fact():
    packages, files = {}, {}
    generic = "linux-headers-" + package.KERNEL
    depends = output(["dpkg-query", "-W", "-f=${Depends}", generic])
    # Ubuntu HWE 的 common 包含 linux-hwe-* 前缀，不能按内核字符串猜包名。
    common = [entry.strip().split(" ", 1)[0] for entry in depends.split(",")
              if re.fullmatch(r"linux-[a-z0-9.+-]*headers-[a-z0-9.+-]+(?:\s+\([^)]*\))?", entry.strip())]
    if not common:
        raise ValueError("目标 generic 头文件没有可确定的 common headers 依赖")
    for name in sorted({generic, *common}):
        status, version = output(["dpkg-query", "-W", "-f=${Status}\t${Version}", name]).split("\t", 1)
        if status != "install ok installed":
            raise ValueError("内核头文件包未完整安装：" + name)
        packages[name] = version
        for filename in output(["dpkg-query", "-L", name]).splitlines():
            path = Path(filename)
            if path.is_file():
                files[filename] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not files:
        raise ValueError("内核头文件集合为空")
    return {"packages": packages, "files_sha256": hashlib.sha256(package.encoded(files)).hexdigest()}


def compile_native(root, artifact, compiler="gcc", jobs=2):
    if os.geteuid() == 0:
        raise ValueError("纯编译入口禁止 root；请以普通用户执行，不会安装或加载驱动")
    if platform.system() != "Linux" or platform.machine() != package.ARCHITECTURE:
        raise ValueError("必须在 Linux x86_64 编译机执行")
    release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    if release.get("ID", "").strip('"') != "ubuntu" or release.get("VERSION_ID", "").strip('"') != "24.04":
        raise ValueError("当前原生目标要求 Ubuntu 24.04")
    root = root.resolve(strict=True)
    artifact = artifact.absolute()
    if (artifact.is_symlink() or artifact.parent.resolve() != artifact.parent
            or not artifact.parent.is_dir() or not artifact.name.endswith(".tar.gz")):
        raise ValueError("输出必须位于已有普通目录且以 .tar.gz 结尾")
    headers = Path("/lib/modules") / package.KERNEL / "build"
    if not headers.is_dir() or (headers / "vmlinux").exists():
        raise ValueError("缺少目标内核头文件，或存在未冻结的 vmlinux BTF 输入")
    kernel_release = (headers / "include/config/kernel.release").read_text().strip()
    if kernel_release != package.KERNEL:
        raise ValueError("头文件真实 kernel.release 与目标不同")
    inputs = package.repository_inputs(root)
    commands = {"gcc": compiler, "make": "make", "ld": "ld", "zstd": "zstd", "python3": sys.executable}
    tools = {name: tool_fact(command) for name, command in commands.items()}
    kernel_headers = header_fact()
    files = {}
    with tempfile.TemporaryDirectory(prefix="ugreen-native-") as temporary:
        source = Path(temporary) / "source"
        shutil.copytree(root / "src/aic8800-1.0.9", source)
        # 清除外部 make/编译选项；明确 CC/LD 与输入记录一致，临时路径不进入调试字节。
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "KBUILD_OUTPUT", "KBUILD_EXTMOD",
                                      "KCFLAGS", "CFLAGS", "CPPFLAGS", "LDFLAGS", "KBUILD_EXTRA_SYMBOLS",
                                      "CROSS_COMPILE", "LLVM", "LLVM_IAS", "CC", "LD", "ARCH"}}
        environment.update(package.BUILD_ENVIRONMENT)
        flags = " ".join(option + "=" + str(source) + "=/usr/src/aic8800-1.0.9"
                         for option in ("-ffile-prefix-map", "-fdebug-prefix-map", "-fmacro-prefix-map"))
        command = ["make", "all", "-j" + str(jobs), "ARCH=x86_64", "CROSS_COMPILE=",
                   "KVER=" + package.KERNEL, "KDIR=" + str(headers.resolve()), "PWD=" + str(source),
                   "CC=" + str(Path(shutil.which(compiler) or compiler).resolve()),
                   "LD=" + str(Path(shutil.which("ld")).resolve()), "KCFLAGS=" + flags]
        subprocess.run(command, cwd=source, env=environment, check=True)
        modules = {}
        for name in ("aic_load_fw", "aic8800_fdrv"):
            data = package.read_file(source / name / (name + ".ko"))
            compressed = subprocess.check_output(["zstd", "-q", "-19", "-T1", "-c"], input=data)
            relative = f"modules/{package.KERNEL}/{package.ARCHITECTURE}/{name}.ko.zst"
            files[relative] = compressed
            modules[name + ".ko.zst"] = package.native_module(compressed)
        for name in inputs:
            if name.startswith("firmware/") or name == "udev/aic.rules":
                files[name] = package.read_file(root / name)
        files["modules-load/aic8800.conf"] = package.MODULES_LOAD
        if (package.repository_inputs(root) != inputs or header_fact() != kernel_headers
                or {name: tool_fact(command) for name, command in commands.items()} != tools):
            raise ValueError("编译期间源码、工具或内核头文件发生变化")
        provenance = {"schema_version": 1, "kernel": package.KERNEL, "architecture": package.ARCHITECTURE,
                      "inputs": inputs, "inputs_sha256": hashlib.sha256(package.encoded(inputs)).hexdigest(),
                      "toolchain": tools, "kernel_headers": kernel_headers, "build_environment": package.BUILD_ENVIRONMENT,
                      "modules": modules, "payload_sha256": {name: hashlib.sha256(data).hexdigest()
                                                              for name, data in sorted(files.items())}}
        package.validate_provenance(provenance, files, root)
        files["native-build.json"] = package.encoded(provenance)
        # 候选文件与目标同文件系统；同路径不同内容拒绝覆盖。
        with tempfile.TemporaryDirectory(prefix=".native-", dir=artifact.parent) as staging:
            candidate = Path(staging) / "native.tar.gz"
            with candidate.open("wb") as stream, gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as zipped:
                with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    for name, data in sorted(files.items()):
                        item = tarfile.TarInfo(package.NATIVE_PREFIX + "/" + name)
                        item.size, item.mode, item.mtime = len(data), 0o644, 0
                        archive.addfile(item, io.BytesIO(data))
            try:
                os.link(candidate, artifact)
            except FileExistsError:
                if artifact.is_symlink() or artifact.read_bytes() != candidate.read_bytes():
                    raise ValueError("已有不同原生归档，拒绝覆盖")
    return {"artifact": str(artifact), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "inputs_sha256": provenance["inputs_sha256"], "kernel": package.KERNEL, "architecture": package.ARCHITECTURE}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--cc", default="gcc", help="目标内核使用的 GCC 可执行文件")
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.jobs <= 64:
        parser.error("--jobs 必须在 1 至 64 之间")
    try:
        print(json.dumps(compile_native(package.ROOT, args.artifact, args.cc, args.jobs), sort_keys=True))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit("compile_native failed: " + str(error)) from error
