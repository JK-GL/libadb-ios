#!/usr/bin/env python3
"""
libadb iOS 构建器

把 adb-mobile 的 ADB 移植编成 iOS 静态库。

设计原则：
1. 每一步都显式建目录，不依赖上游脚本的隐式行为
2. 版本锚点是硬约束，不符就立刻失败，不做猜测性重试
3. 失败时输出能直接定位问题的信息

用法：
    python3 scripts/build_libadb.py --root <源码根> --stage all
    python3 scripts/build_libadb.py --root <源码根> --stage protobuf
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---- 版本锚点 ----
ANDROID_TOOLS_TAG = "35.0.2"   # porting/*.cpp 针对 platform-tools 35.0.2 改造
PROTOBUF_VERSION = "28.3"      # 28.x 只引 absl_log.h，不会与 adb_trace.h 的 LOG 宏冲突
TARGET = "adb/iphoneos/arm64"   # 上游格式：<lib>/<sdk>/<arch>，lib 段决定 LIB_NAME

ROOT = Path(os.environ.get("LIBADB_ROOT", Path.cwd())).resolve()
ANDROID_TOOLS: Path = ROOT / "android-tools"
PROTOBUF_SRC: Path = ROOT / "external" / "protobuf"
OUTPUT: Path = ROOT / "output"
# 产物实际落在 output/<sdk>/<arch>/
_SDK, _ARCH = TARGET.split("/")[1], TARGET.split("/")[2]
FULL_OUTPUT: Path = OUTPUT / _SDK / _ARCH


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
            print((p.stdout or "")[-3000:])
            print((p.stderr or "")[-3000:])
        die(f"命令失败（退出码 {p.returncode}）")
    return p


def ensure_dirs() -> None:
    """defines.sh 里用的是 `cd $OUTPUT` 而不是 mkdir -p，所以目录必须预先存在。"""
    for d in (OUTPUT, FULL_OUTPUT):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 步骤

def step_pin() -> None:
    """android-tools 钉到 35.0.2。submodule 跟 master 会拉到 37.x，与 porting 代码不匹配。"""
    ensure_dirs()
    if not ANDROID_TOOLS.is_dir():
        die(f"{ANDROID_TOOLS} 不存在，submodule 未检出")

    log(f"钉住 android-tools -> {ANDROID_TOOLS_TAG}")
    run(["git", "fetch", "--tags", "--depth", "1", "origin",
         f"refs/tags/{ANDROID_TOOLS_TAG}:refs/tags/{ANDROID_TOOLS_TAG}"],
        cwd=ANDROID_TOOLS, check=False)
    run(["git", "checkout", "-f", f"tags/{ANDROID_TOOLS_TAG}"], cwd=ANDROID_TOOLS)
    run(["git", "submodule", "update", "--init", "--recursive", "--force"], cwd=ANDROID_TOOLS)

    ver = run(["git", "describe", "--tags", "--always"], cwd=ANDROID_TOOLS, capture=True).stdout.strip()
    log(f"android-tools = {ver}")
    if ANDROID_TOOLS_TAG not in ver:
        die(f"版本不符：期望 {ANDROID_TOOLS_TAG}，实际 {ver}")


def step_protoc() -> None:
    """protoc 必须与运行库同版本，否则 .pb.cc 与库 ABI 不符。"""
    exe = shutil.which("protoc")
    if not exe:
        die("protoc 未安装")
    ver = run([exe, "--version"], capture=True).stdout.strip().split()[-1]
    log(f"protoc = {ver}")
    if ver != PROTOBUF_VERSION:
        die(
            f"protoc 版本 {ver} != {PROTOBUF_VERSION}。"
            f"protobuf 29+ 的 descriptor.h 会引入 absl/log/log.h（裸 LOG 宏），"
            f"与 adb 的 adb_trace.h 冲突，报 'STATELESS 未声明'。"
        )


def step_protobuf() -> None:
    """编 iOS 版 protobuf 28.3 静态库。"""
    ensure_dirs()
    log("编译 protobuf 28.3 (iOS)")
    script = ROOT / "porting" / "scripts" / "make-protobuf.sh"
    if not script.is_file():
        die(f"缺少 {script}")
    run(["bash", str(script)], cwd=ROOT / "porting",
        env=dict(os.environ, TARGET=TARGET, OUTPUT=str(OUTPUT)))


def step_patch_cmake() -> None:
    """
    阻止 CMake 用 CONFIG 模式找到 Homebrew 的 protobuf 36.x。

    上游 vendor/CMakeLists.txt 的 `find_package(Protobuf CONFIG)` 会先把
    /opt/homebrew 拉进 include path，之后 protobuf 的头文件就会把 absl 的
    LOG 宏带进来，与 adb_trace.h 里同名宏直接冲突。
    """
    ensure_dirs()
    f = ANDROID_TOOLS / "vendor" / "CMakeLists.txt"
    if not f.is_file():
        die(f"缺少 {f}")
    text = f.read_text(encoding="utf-8", errors="ignore")
    if "LIBADB_NO_PROTOBUF_CONFIG" in text:
        log("CMakeLists.txt 已 patch，跳过")
        return

    anchor = "find_package(Protobuf CONFIG)"
    if anchor not in text:
        die(f"patch 锚点 '{anchor}' 不存在，上游结构可能已变化")
    text = text.replace(
        anchor,
        "# LIBADB_NO_PROTOBUF_CONFIG: 禁 CONFIG 模式，避免命中 Homebrew protobuf 36.x\n"
        "set(Protobuf_NO_CONFIG ON)\n"
        "set(protobuf_MODULE_COMPATIBLE ON)",
        1,
    )
    f.write_text(text, encoding="utf-8")
    log("CMakeLists.txt patch 完成")


def step_patch_makeadb() -> None:
    """给 make-adb.sh 的 cmake 调用注入本地 protobuf 路径。"""
    ensure_dirs()
    f = ROOT / "porting" / "scripts" / "make-adb.sh"
    if not f.is_file():
        die(f"缺少 {f}")
    text = f.read_text(encoding="utf-8")
    if "LIBADB_PROTOBUF_PATHS" in text:
        log("make-adb.sh 已 patch，跳过")
        return

    # 上游第 40 行精确为 '\t$CMAKE_COMPAT_FLAGS ..'
    anchor = "\t$CMAKE_COMPAT_FLAGS .."
    if anchor not in text:
        die(f"patch 锚点 {anchor!r} 不存在")

    inject = (
        "\t-DLIBADB_PROTOBUF_PATHS=1 \\\n"
        '\t-DProtobuf_INCLUDE_DIR="$SOURCE_ROOT/external/protobuf/src" \\\n'
        '\t-DProtobuf_INCLUDE_DIRS="$SOURCE_ROOT/external/protobuf/src" \\\n'
        '\t-DProtobuf_LIBRARY="$FULL_OUTPUT/libprotobuf.a" \\\n'
        '\t-DProtobuf_PROTOC_EXECUTABLE="$(command -v protoc)" \\\n'
        + anchor
    )
    f.write_text(text.replace(anchor, inject, 1), encoding="utf-8")
    log("make-adb.sh patch 完成")


def step_adb() -> None:
    ensure_dirs()
    log("编译 libadb (iOS)")
    run(["bash", str(ROOT / "porting" / "scripts" / "make-adb.sh")],
        cwd=ROOT / "porting",
        env=dict(os.environ, TARGET=TARGET, OUTPUT=str(OUTPUT)))


def step_collect() -> Path:
    """合并所有 .a 并校验对外符号。"""
    ensure_dirs()
    libs = sorted(FULL_OUTPUT.glob("*.a"))
    if not libs:
        die(f"{FULL_OUTPUT} 下无 .a 产物")
    log(f"找到 {len(libs)} 个静态库")
    for p in libs:
        log(f"  {p.name}  {p.stat().st_size:,} B")

    dist = ROOT / "dist"
    inc = dist / "include"
    inc.mkdir(parents=True, exist_ok=True)

    out = dist / "libadb-full.a"
    run(["libtool", "-static", "-o", str(out)] + [str(p) for p in libs])

    hdr_dir = ROOT / "porting" / "adb" / "include"
    copied = 0
    for h in hdr_dir.rglob("*.h"):
        rel = h.relative_to(hdr_dir)
        dst = inc / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(h, dst)
        copied += 1
    log(f"复制 {copied} 个头文件")

    size = out.stat().st_size
    log(f"libadb-full.a = {size:,} B")
    if size < 500_000:
        log(f"⚠️ 产物偏小（{size:,} B），可能链接不完整")

    nm = run(["nm", "-gU", str(out)], capture=True, check=False)
    syms = nm.stdout or ""
    for sym in ["adb_commandline_porting", "adb_set_home", "adb_set_server_port"]:
        ok = sym in syms
        log(f"符号 {sym}: {'✅' if ok else '❌'}")
        if not ok:
            die(f"关键符号 {sym} 缺失")
    return out


# ---------------------------------------------------------------- 入口

STAGES = {
    "pin": step_pin,
    "protoc": step_protoc,
    "protobuf": step_protobuf,
    "patch": lambda: (step_patch_cmake(), step_patch_makeadb()),
    "adb": step_adb,
    "collect": step_collect,
}
ORDER = ["pin", "protoc", "protobuf", "patch", "adb", "collect"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all", choices=["all"] + ORDER)
    ap.add_argument("--root", default=None)
    args = ap.parse_args()

    global ROOT, ANDROID_TOOLS, PROTOBUF_SRC, OUTPUT, FULL_OUTPUT
    if args.root:
        ROOT = Path(args.root).resolve()
        ANDROID_TOOLS = ROOT / "android-tools"
        PROTOBUF_SRC = ROOT / "external" / "protobuf"
        OUTPUT = ROOT / "output"
        FULL_OUTPUT = OUTPUT / TARGET.split("/")[1] / TARGET.split("/")[2]

    log(f"ROOT = {ROOT}")
    if TARGET.count("/") != 2:
        die(f"TARGET 格式错误：{TARGET!r}，必须是 <lib>/<sdk>/<arch>")
    if not (ROOT / "porting").is_dir():
        die(f"{ROOT} 下没有 porting/，请用 --root 指定 adb-mobile 源码根")
    ensure_dirs()

    t0 = time.time()
    stages = ORDER if args.stage == "all" else [args.stage]
    for st in stages:
        log(f"===== stage: {st} =====")
        STAGES[st]()
    log(f"完成，耗时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
