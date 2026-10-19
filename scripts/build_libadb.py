#!/usr/bin/env python3
"""
libadb iOS 构建器

用途：把 adb-mobile 的 ADB 移植在 macOS 上编成 iOS 静态库。
所有步骤显式、可复现，失败时给出明确根因，不做猜测性重试。

用法：
    python3 scripts/build_libadb.py --stage all
    python3 scripts/build_libadb.py --stage protobuf   # 只编 protobuf
    python3 scripts/build_libadb.py --stage adb        # 只编 adb
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---- 版本锚点：与 adb-mobile 的移植代码对齐，不要随意改动 ----
ANDROID_TOOLS_TAG = "35.0.2"   # porting/*.cpp 是针对 platform-tools 35.0.2 改的
PROTOBUF_VERSION = "28.3"      # 28.x 的 descriptor.h 只引 absl_log.h，不会和 adb 的 LOG 宏打架
TARGET = "iphoneos/arm64"

# 项目根：由 --root 注入，默认取当前工作目录（CI 里是 src/）
ROOT = Path(os.environ.get("LIBADB_ROOT", Path.cwd())).resolve()
ANDROID_TOOLS = ROOT / "android-tools"
PROTOBUF_SRC = ROOT / "external" / "protobuf"
OUTPUT = ROOT / "output"
FULL_OUTPUT = OUTPUT / TARGET


def log(msg: str) -> None:
    print(f"\033[36m[build]\033[0m {msg}", flush=True)


def die(msg: str) -> None:
    print(f"\033[31m[fail]\033[0m {msg}", flush=True)
    sys.exit(1)


def run(cmd, cwd=None, check=True, capture=False, env=None):
    log(f"$ {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    kwargs = dict(cwd=cwd, shell=isinstance(cmd, str), env=env)
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    p = subprocess.run(cmd, **kwargs)
    if check and p.returncode != 0:
        if capture:
            print(p.stdout[-3000:] if p.stdout else "")
            print(p.stderr[-3000:] if p.stderr else "")
        die(f"命令失败（退出码 {p.returncode}）")
    return p


def step_pin_android_tools() -> None:
    """把 android-tools 钉到 35.0.2。submodule 默认跟 master，会拉到 37.x 导致源码对不上。"""
    log(f"钉住 android-tools -> {ANDROID_TOOLS_TAG}")
    if not ANDROID_TOOLS.is_dir():
        die("android-tools 目录不存在，检查 submodule 是否已检出")

    run(["git", "fetch", "--tags", "--depth", "1", "origin",
         f"refs/tags/{ANDROID_TOOLS_TAG}:refs/tags/{ANDROID_TOOLS_TAG}"],
        cwd=ANDROID_TOOLS, check=False)
    run(["git", "checkout", "-f", f"tags/{ANDROID_TOOLS_TAG}"], cwd=ANDROID_TOOLS)
    run(["git", "submodule", "update", "--init", "--recursive", "--force"], cwd=ANDROID_TOOLS)

    ver = run(["git", "describe", "--tags", "--always"], cwd=ANDROID_TOOLS,
              capture=True).stdout.strip()
    log(f"android-tools 当前版本: {ver}")
    if ANDROID_TOOLS_TAG not in ver:
        die(f"版本钉失败，期望 {ANDROID_TOOLS_TAG}，实际 {ver}")


def step_verify_protoc() -> str:
    """protoc 必须与 PROTOBUF_VERSION 一致，否则生成的 .pb.cc 和运行库 ABI 不符。"""
    exe = shutil.which("protoc")
    if not exe:
        die("protoc 未安装。macOS 上执行: brew install protobuf@28 或下载 28.3 官方二进制")
    out = run([exe, "--version"], capture=True).stdout.strip()
    ver = out.split()[-1]
    log(f"protoc = {ver}")
    if ver != PROTOBUF_VERSION:
        die(f"protoc 版本不符：期望 {PROTOBUF_VERSION}，实际 {ver}。"
            f"新版 protobuf 会引入 absl/log/log.h 的裸 LOG 宏，与 adb_trace.h 冲突。")
    return exe


def step_build_protobuf() -> None:
    """编译 iOS 版 protobuf 28.3，产物进 output/iphoneos/arm64/。"""
    log("编译 protobuf 28.3 (iOS)")
    script = ROOT / "porting" / "scripts" / "make-protobuf.sh"
    if not script.is_file():
        die(f"找不到 {script}")
    env = dict(os.environ, TARGET=TARGET, OUTPUT=str(OUTPUT))
    run(["bash", str(script)], cwd=ROOT / "porting", env=env)


def step_patch_cmake() -> None:
    """
    让 adb 的 CMake 用我们自己编的 protobuf 28.3，而不是 Homebrew 的 36.x。

    上游 vendor/CMakeLists.txt 里 `find_package(Protobuf CONFIG)` 会优先命中
    Homebrew 的 protobuf-config.cmake，从而把 /opt/homebrew/include 拉进 include path，
    进而引入 absl/log/log.h 的 LOG 宏，与 adb_trace.h 的 LOG 冲突。
    """
    log("patch vendor/CMakeLists.txt：强制使用本地 protobuf")
    f = ANDROID_TOOLS / "vendor" / "CMakeLists.txt"
    if not f.is_file():
        die(f"找不到 {f}")
    text = f.read_text(encoding="utf-8", errors="ignore")
    if "ADB_MOBILE_FORCE_LOCAL_PROTOBUF" in text:
        log("已 patch 过，跳过")
        return

    patched = text.replace(
        "find_package(Protobuf CONFIG)",
        "# ADB_MOBILE_FORCE_LOCAL_PROTOBUF: 禁用 CONFIG 模式，避免命中 Homebrew 的 protobuf 36.x\n"
        "set(Protobuf_NO_CONFIG ON)\n"
        "set(protobuf_MODULE_COMPATIBLE ON)",
    )
    if patched == text:
        die("patch 锚点未命中，vendor/CMakeLists.txt 结构可能已变化")
    f.write_text(patched, encoding="utf-8")
    log("patch 完成")


def step_patch_make_adb() -> None:
    """给 make-adb.sh 的 cmake 调用补上 protobuf 路径。"""
    log("patch make-adb.sh：注入 protobuf 路径")
    f = ROOT / "porting" / "scripts" / "make-adb.sh"
    text = f.read_text(encoding="utf-8")
    if "ADB_MOBILE_PROTOBUF_PATHS" in text:
        log("已 patch 过，跳过")
        return

    anchor = "\t$CMAKE_COMPAT_FLAGS .."
    inject = (
        '\t-DADB_MOBILE_PROTOBUF_PATHS=1 \\\n'
        '\t-DProtobuf_INCLUDE_DIR="$SOURCE_ROOT/external/protobuf/src" \\\n'
        '\t-DProtobuf_LIBRARY="$FULL_OUTPUT/libprotobuf.a" \\\n'
        '\t-DProtobuf_PROTOC_EXECUTABLE="$(command -v protoc)" \\\n'
        + anchor
    )
    if anchor not in text:
        die("make-adb.sh 里找不到 cmake 锚点，上游脚本可能已变化")
    f.write_text(text.replace(anchor, inject), encoding="utf-8")
    log("patch 完成")


def step_build_adb() -> None:
    log("编译 libadb (iOS)")
    env = dict(os.environ, TARGET=TARGET, OUTPUT=str(OUTPUT))
    run(["bash", str(ROOT / "porting" / "scripts" / "make-adb.sh")],
        cwd=ROOT / "porting", env=env)


def step_collect() -> Path:
    """把散落的 .a 合并成一个 libadb-full.a。"""
    log("合并静态库")
    libs = sorted(FULL_OUTPUT.glob("*.a"))
    if not libs:
        die(f"{FULL_OUTPUT} 下没有任何 .a，构建实际未产出")
    log(f"找到 {len(libs)} 个静态库: {[p.name for p in libs]}")

    dist = ROOT / "dist"
    (dist / "include").mkdir(parents=True, exist_ok=True)
    out = dist / "libadb-full.a"
    run(["libtool", "-static", "-o", str(out)] + [str(p) for p in libs])

    for h in (ROOT / "porting" / "adb" / "include").glob("*.h"):
        shutil.copy2(h, dist / "include" / h.name)

    size = out.stat().st_size
    log(f"libadb-full.a = {size:,} B")
    if size < 500_000:
        log(f"⚠️ 产物偏小（{size} B），可能链接不完整")

    # 符号抽查：确认对外接口真的在
    nm = run(["nm", "-gU", str(out)], capture=True, check=False)
    wanted = ["adb_commandline_porting", "adb_set_home", "adb_set_server_port"]
    for sym in wanted:
        found = sym in (nm.stdout or "")
        log(f"符号 {sym}: {'✅' if found else '❌'}")
        if not found:
            die(f"关键符号 {sym} 缺失，库不可用")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all",
                    choices=["all", "pin", "protoc", "protobuf", "patch", "adb", "collect"])
    ap.add_argument("--root", default=None, help="adb-mobile 源码根目录，默认当前目录")
    args = ap.parse_args()

    global ROOT, ANDROID_TOOLS, PROTOBUF_SRC, OUTPUT, FULL_OUTPUT
    if args.root:
        ROOT = Path(args.root).resolve()
        ANDROID_TOOLS = ROOT / "android-tools"
        PROTOBUF_SRC = ROOT / "external" / "protobuf"
        OUTPUT = ROOT / "output"
        FULL_OUTPUT = OUTPUT / TARGET
    log(f"ROOT = {ROOT}")
    if not (ROOT / "porting").is_dir():
        die(f"{ROOT} 下没有 porting/，不是 adb-mobile 源码根。请用 --root 指定。")

    t0 = time.time()
    if args.stage in ("all", "pin"):
        step_pin_android_tools()
    if args.stage in ("all", "protoc"):
        step_verify_protoc()
    if args.stage in ("all", "protobuf"):
        step_build_protobuf()
    if args.stage in ("all", "patch"):
        step_patch_cmake()
        step_patch_make_adb()
    if args.stage in ("all", "adb"):
        step_build_adb()
    if args.stage in ("all", "collect"):
        step_collect()

    log(f"完成，耗时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
