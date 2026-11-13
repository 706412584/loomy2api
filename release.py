"""loomy2api 一键发布脚本（纯标准库，无第三方依赖）。

用法::

    python release.py                      # 只构建 + 打包 + 验签，不发布
    python release.py --version 2.8.0      # 指定版本号（默认 patch +1）
    python release.py --publish            # 构建后提交、推送并创建 GitHub Release
    python release.py --notes-file notes.md --publish

发布内容（全部上传到公开仓库 706412584/loomy2api-releases）：

* ``loomy2api-full-vX.Y.Z.apk``   Android 全量版（含接码注册）
* ``loomy2api-lite-vX.Y.Z.apk``   Android 精简版
* ``loomy2api-vX.Y.Z.zip``        Windows / Linux 源码包
* ``SHA256SUMS.txt``              全部资产校验和

版本号三处同步：``android/app/build.gradle.kts``（versionCode/versionName）、
``pyproject.toml``、``loomy2api/__init__.py``。

安全约定：
* 默认（仅构建）模式**不修改任何版本号文件、不提交、不推送、不建 release**，
  只构建当前版本并把产物放进 ``dist/``；
* ``--publish`` 模式才写入版本号、提交、打 tag、推送并创建 release，
  且 push 与 release 创建前都会打印命令并等待确认；
* 所有 ``gh`` 调用清除代理环境变量（GitHub 直连，避免本机代理误报 token 失效）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"

RELEASE_REPO = "706412584/loomy2api-releases"
RELEASE_BRANCH = "main"

GRADLE_FILE = ROOT / "android" / "app" / "build.gradle.kts"
PYPROJECT = ROOT / "pyproject.toml"
INIT_FILE = ROOT / "loomy2api" / "__init__.py"
KEYSTORE = ROOT / "android" / "loomy-release.jks"
LOCAL_PROPS = ROOT / "android" / "local.properties"

APK_FULL = ROOT / "android" / "app" / "build" / "outputs" / "apk" / "full" / "release" / "app-full-release.apk"
APK_LITE = ROOT / "android" / "app" / "build" / "outputs" / "apk" / "lite" / "release" / "app-lite-release.apk"

#: 源码 zip 的白名单 —— 只有这里列出的文件/目录会进包。
#: 运行态文件（accounts.json、config.json、logs/、*.bak*）一律不包含。
ZIP_FILES = [
    "pyproject.toml",
    "README.md",
    "README.zh-CN.md",
    "LICENSE",
    "config.example.json",
    "accounts.example.json",
    "start.cmd",
    "stop.cmd",
    "一键启动.bat",
    "一键停止.bat",
    "Dockerfile",
    "docker-compose.yml",
]
ZIP_DIRS = ["loomy2api", "docs"]
#: 目录内只收集这些扩展名，双保险防止运行态/密钥文件被打进公开 zip。
ZIP_DIR_SUFFIXES = {".py", ".html", ".md", ".typed"}


# --------------------------------------------------------------------------- utils


class ReleaseError(RuntimeError):
    pass


def step(msg: str) -> None:
    print(f"\n\033[36m==>\033[0m {msg}", flush=True)


def info(msg: str) -> None:
    print(f"    {msg}", flush=True)


def run(cmd, *, cwd=ROOT, env=None, capture=False, check=True):
    printable = " ".join(str(c) for c in cmd)
    info(f"$ {printable}")
    result = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd),
        env=env,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        if capture:
            sys.stderr.write(result.stdout or "")
            sys.stderr.write(result.stderr or "")
        raise ReleaseError(f"命令失败（退出码 {result.returncode}）：{printable}")
    return result


def clean_env_for_github() -> dict:
    """GitHub API 直连：本机代理会导致 TLS 握手失败，进而被 gh 误报为 token 失效。"""
    env = os.environ.copy()
    for key in list(env):
        if key.lower() in ("http_proxy", "https_proxy", "all_proxy"):
            env.pop(key, None)
    return env


def gh(args, **kwargs):
    return run(["gh", *args], env=clean_env_for_github(), **kwargs)


def confirm(prompt: str) -> None:
    if not sys.stdin.isatty():
        raise ReleaseError("非交互环境：发布确认无法读取，请人工执行。")
    answer = input(f"\n{prompt} [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        raise ReleaseError("已取消。")


# --------------------------------------------------------------------------- version


def read_current_version() -> tuple[str, int]:
    text = GRADLE_FILE.read_text(encoding="utf-8")
    code = re.search(r"versionCode\s*=\s*(\d+)", text)
    name = re.search(r'versionName\s*=\s*"([^"]+)"', text)
    if not code or not name:
        raise ReleaseError("无法从 build.gradle.kts 读取 versionCode / versionName")
    return name.group(1), int(code.group(1))


def bump_version(version: str, code: int) -> None:
    for path, pattern, repl in (
        (GRADLE_FILE, r"versionCode\s*=\s*\d+", f"versionCode = {code}"),
        (GRADLE_FILE, r'versionName\s*=\s*"[^"]+"', f'versionName = "{version}"'),
        (PYPROJECT, r'version\s*=\s*"[^"]+"', f'version = "{version}"'),
        (INIT_FILE, r'__version__\s*=\s*"[^"]+"', f'__version__ = "{version}"'),
    ):
        text = path.read_text(encoding="utf-8")
        new, n = re.subn(pattern, repl, text, count=1)
        if n != 1:
            raise ReleaseError(f"版本号替换失败：{path}")
        path.write_text(new, encoding="utf-8")
    info(f"版本号 → {version} (versionCode {code})")


def next_patch(version: str) -> str:
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ReleaseError(f"当前版本号不是 x.y.z 形式：{version}")
    parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts)


# --------------------------------------------------------------------------- checks


def check_git_clean() -> None:
    out = run(["git", "status", "--porcelain"], capture=True).stdout.strip()
    if out:
        raise ReleaseError(
            "工作区不干净，请先提交或暂存以下改动再发布：\n" + out
        )


def check_gh_auth() -> None:
    gh(["auth", "status"], capture=True)
    info("gh 已登录")


def check_keystore() -> None:
    if not KEYSTORE.exists():
        raise ReleaseError(f"缺少签名密钥 {KEYSTORE}（发布版不能用 debug 签名）")
    info("签名密钥存在")


def check_latest_release(version: str) -> None:
    tag = f"v{version}"
    result = gh(
        ["release", "view", tag, "--repo", RELEASE_REPO, "--json", "tagName"],
        capture=True, check=False,
    )
    if result.returncode == 0:
        raise ReleaseError(f"远端已存在 {tag}，请提升版本号")


def find_apksigner() -> Path:
    sdk = ""
    if LOCAL_PROPS.exists():
        m = re.search(r"sdk\.dir\s*=\s*(.+)", LOCAL_PROPS.read_text(encoding="utf-8"))
        if m:
            sdk = m.group(1).strip().replace("\\\\", "\\").replace("\\:", ":")
    sdk = sdk or os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME") or ""
    candidates = []
    if sdk:
        bt = Path(sdk) / "build-tools"
        if bt.is_dir():
            for ver in sorted(bt.iterdir(), reverse=True):
                candidates.append(ver / "apksigner.bat")
    found = shutil.which("apksigner")
    if found:
        candidates.insert(0, Path(found))
    for c in candidates:
        if c.exists():
            return c
    raise ReleaseError("找不到 apksigner（检查 android/local.properties 的 sdk.dir）")


def find_java17() -> Path | None:
    """Chaquopy/AGP 需要 JDK17；当前 shell 默认可能是 1.8。"""
    java_home = os.environ.get("JAVA_HOME")
    if java_home and (Path(java_home) / "bin" / "java.exe").exists():
        return Path(java_home)
    for base in (Path("D:/Android/openjdk17"), Path("C:/Program Files/Java")):
        if base.is_dir():
            for sub in sorted(base.iterdir()):
                exe = sub / "bin" / "java.exe"
                if exe.exists():
                    return sub
    return None


# --------------------------------------------------------------------------- test & build


def run_tests() -> None:
    step("运行单元测试")
    run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."])


def build_apks() -> None:
    step("构建 Android full + lite release APK")
    env = os.environ.copy()
    jdk = find_java17()
    if jdk:
        env["JAVA_HOME"] = str(jdk)
        env["PATH"] = str(jdk / "bin") + os.pathsep + env.get("PATH", "")
        info(f"JAVA_HOME = {jdk}")
    run(
        ["cmd.exe", "/c", "gradlew.bat", ":app:assembleFullRelease", ":app:assembleLiteRelease"],
        cwd=ROOT / "android", env=env,
    )
    for apk in (APK_FULL, APK_LITE):
        if not apk.exists():
            raise ReleaseError(f"构建产物缺失：{apk}")


def verify_signature(apk: Path, apksigner: Path) -> None:
    result = run(
        [str(apksigner), "verify", "--print-certs", str(apk)],
        capture=True, env=clean_env_for_github(),
    )
    out = result.stdout + result.stderr
    if "CN=Loomy2API" not in out:
        raise ReleaseError(f"{apk.name} 签名不符（期望 CN=Loomy2API）：\n{out}")
    info(f"{apk.name} 签名正确")


# --------------------------------------------------------------------------- packaging


def collect_zip_sources():
    files = []
    for rel in ZIP_FILES:
        p = ROOT / rel
        if p.exists():
            files.append(p)
    for rel in ZIP_DIRS:
        for p in sorted((ROOT / rel).rglob("*")):
            if not p.is_file():
                continue
            if "__pycache__" in p.parts or p.suffix not in ZIP_DIR_SUFFIXES:
                continue
            files.append(p)
    return files


def build_source_zip(version: str) -> Path:
    step("打包 Windows / Linux 源码 zip")
    DIST.mkdir(exist_ok=True)
    out = DIST / f"loomy2api-v{version}.zip"
    prefix = f"loomy2api-v{version}"
    files = collect_zip_sources()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, f"{prefix}/{p.relative_to(ROOT).as_posix()}")
    with zipfile.ZipFile(out) as zf:
        bad = zf.testzip()
        if bad:
            raise ReleaseError(f"zip 校验失败：{bad}")
        names = zf.namelist()
        for forbidden in ("accounts.json", "config.json", "gateway.lock"):
            if any(n.endswith("/" + forbidden) for n in names):
                raise ReleaseError(f"zip 中不应包含运行态文件：{forbidden}")
    info(f"{out.name}：{len(files)} 个文件，{out.stat().st_size / 1024:.0f} KB")
    return out


def stage_apks(version: str) -> list[Path]:
    DIST.mkdir(exist_ok=True)
    staged = []
    for src, flavor in ((APK_FULL, "full"), (APK_LITE, "lite")):
        dst = DIST / f"loomy2api-{flavor}-v{version}.apk"
        shutil.copy2(src, dst)
        staged.append(dst)
    return staged


def write_sha256sums(assets: list[Path]) -> Path:
    out = DIST / "SHA256SUMS.txt"
    lines = []
    for a in assets:
        digest = hashlib.sha256(a.read_bytes()).hexdigest()
        lines.append(f"{digest}  {a.name}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    info("SHA256SUMS.txt 已生成")
    return out


def draft_release_notes(version: str, previous: str) -> Path:
    """没有 --notes-file 时，用上个版本以来的 git log 生成草稿。

    自动排除 chore(release) 之类的版本号提交，只留下实际改动。
    """
    out = DIST / "notes.md"
    log = run(
        ["git", "log", "--format=- %s", f"v{previous}..HEAD"],
        capture=True, check=False,
    ).stdout.strip()
    if not log:  # 源码仓库可能没有打 tag，退化为最近若干条
        log = run(["git", "log", "--format=- %s", "-15"], capture=True).stdout.strip()
    lines = [l for l in log.splitlines() if "chore(release)" not in l]
    body = f"## loomy2api v{version}\n\n" + ("\n".join(lines) if lines else "- （请补充发布说明）")
    out.write_text(body + "\n", encoding="utf-8")
    info(f"发布说明草稿已写入 {out}（可用 --notes-file 覆盖）")
    return out


# --------------------------------------------------------------------------- publish


def commit_and_push(version: str, code: int) -> None:
    step("提交版本号并推送")
    run(["git", "add", str(GRADLE_FILE), str(PYPROJECT), str(INIT_FILE)])
    run(["git", "diff", "--cached", "--stat"])
    confirm(f"将提交 \"chore(release): bump to {version} (versionCode {code})\" 并推送到 origin/{RELEASE_BRANCH}，继续？")
    run(["git", "commit", "-m", f"chore(release): bump to {version} (versionCode {code})"])
    # 在源码仓库打 tag，供下次发布生成 release notes 的区间。
    run(["git", "tag", "-a", f"v{version}", "-m", f"loomy2api v{version}"])
    run(["git", "push", "origin", RELEASE_BRANCH])
    run(["git", "push", "origin", f"v{version}"])


def create_release(version: str, assets: list[Path], notes: Path) -> None:
    step("创建 GitHub Release")
    tag = f"v{version}"
    cmd = [
        "release", "create", tag,
        "--repo", RELEASE_REPO,
        "--target", RELEASE_BRANCH,
        "--title", tag,
        "--notes-file", str(notes),
        *[str(a) for a in assets],
    ]
    info("资产清单：")
    for a in assets:
        info(f"  {a.name}  ({a.stat().st_size / 1048576:.1f} MB)")
    confirm(f"将向公开仓库 {RELEASE_REPO} 发布 {tag}，此操作对外可见，继续？")
    gh(cmd)
    info(f"发布完成：https://github.com/{RELEASE_REPO}/releases/tag/{tag}")


# --------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description="loomy2api 一键发布")
    parser.add_argument("--version", help="目标版本号 x.y.z（默认当前 patch +1）")
    parser.add_argument("--publish", action="store_true", help="提交、推送并创建 Release（默认只构建）")
    parser.add_argument("--notes-file", type=Path, help="发布说明 markdown 文件")
    parser.add_argument("--skip-tests", action="store_true", help="跳过单元测试（不建议）")
    args = parser.parse_args()

    try:
        current, code = read_current_version()
        version = args.version or next_patch(current)
        new_code = code + 1
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ReleaseError(f"版本号格式应为 x.y.z：{version}")
        step(f"loomy2api 发布流程：{current} → {version} (versionCode {new_code})"
             + ("  [发布模式]" if args.publish else "  [仅构建]"))

        step("前置检查")
        check_git_clean()
        check_keystore()
        check_gh_auth()
        if args.publish:
            check_latest_release(version)
        apksigner = find_apksigner()
        info(f"apksigner = {apksigner}")

        if not args.skip_tests:
            run_tests()

        if args.publish:
            # 只有发布模式才写入版本号；仅构建模式不动工作区，
            # 避免「先构建后发布」时版本号被二次递增。
            bump_version(version, new_code)
        else:
            info("仅构建：不修改版本号文件，产物沿用当前版本号 "
                 f"{current}（版本名带 * 标记的以实际 APK 为准）")
            version, new_code = current, code

        build_apks()

        step("验签")
        for apk in (APK_FULL, APK_LITE):
            verify_signature(apk, apksigner)

        step("整理发布资产 → dist/")
        staged = stage_apks(version)
        source_zip = build_source_zip(version)
        assets = [*staged, source_zip]
        assets.append(write_sha256sums(assets))
        notes = args.notes_file or draft_release_notes(version, current)
        info(f"发布说明：{notes}")

        if args.publish:
            commit_and_push(version, new_code)
            create_release(version, assets, notes)
        else:
            step("构建完成（未发布，工作区未改动）")
            info(f"产物位于 dist/：v{version}")
            info("要发布新版本，请先提交功能改动，然后执行：")
            info(f"  python release.py --version {next_patch(current)} --publish")
        return 0
    except ReleaseError as exc:
        print(f"\n\033[31m发布中止：{exc}\033[0m", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
