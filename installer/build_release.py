"""Build the fork's release: wheel + sdist, then a Setup.exe that installs like upstream's.

    python installer/build_release.py [--base-runtime DIR] [--iscc PATH] [--skip-build]

The Setup.exe carries a self-contained Python runtime. It is built from an existing
3dsmax-mcp runtime (by default the one upstream's 1.7.5 installer puts in
C:\\Program Files\\3dsmax-mcp\\runtime): the embedded Python 3.12 and its packages are kept,
and upstream's maxmcp package is replaced by this checkout's wheel. The dependencies are the
same as upstream 1.7.5's (mcp[cli] < 2), so nothing is downloaded.

Writes installer/stage (staging), installer/output (Setup.exe and its .sha256) and dist/.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_BASE = Path(r"C:\Program Files\3dsmax-mcp\runtime")
DEFAULT_ISCC = [Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"), Path(r"C:\Program Files\Inno Setup 6\ISCC.exe")]


def version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE).group(1)


def display_version(v: str) -> str:
    return v.replace("+", "-")  # 1.7.5+fork.1 -> 1.7.5-fork.1


def build_dists(out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(["uv", "build", "--offline", "--out-dir", str(out)], cwd=ROOT, check=True)
    wheels = sorted(out.glob(f"3dsmax_mcp-{version()}-*.whl"))
    if not wheels:
        sys.exit(f"no wheel for {version()} in {out}")
    return wheels[-1]


def stage_runtime(base: Path, wheel: Path, dest: Path) -> None:
    site = Path("Lib") / "site-packages"
    if not (base / "python.exe").exists() or not (base / site).is_dir():
        sys.exit(f"{base} is not a 3dsmax-mcp runtime (python.exe and Lib/site-packages expected)")

    def skip(directory: str, names: list[str]) -> list[str]:
        rel = Path(directory).resolve().relative_to(base.resolve())
        drop = {n for n in names if n == "__pycache__"}
        if rel == site:
            drop |= {n for n in names if n == "maxmcp" or (n.startswith("3dsmax_mcp-") and n.endswith(".dist-info"))}
        return sorted(drop)

    shutil.copytree(base, dest, ignore=skip)
    with zipfile.ZipFile(wheel) as zf:
        zf.extractall(dest / site)


def stage(base: Path, wheel: Path, stage_dir: Path) -> None:
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    app = stage_dir / "app"
    stage_runtime(base, wheel, app / "runtime")
    shutil.copy2(HERE / "configure.py", app / "configure.py")
    shutil.copy2(HERE / "app.ico", app / "app.ico")
    shutil.copy2(ROOT / "LICENSE", app / "LICENSE.txt")
    docs = app / "docs"
    docs.mkdir()
    for src in (ROOT / "README.md", ROOT / "docs" / "FORK.md", ROOT / "docs" / "CHANGELOG.md",
                HERE / "WHAT-THIS-FORK-DOES.txt", ROOT / "RELEASE_NOTES.md"):
        if src.exists():
            shutil.copy2(src, docs / src.name)
    sys.path.insert(0, str(ROOT))
    import install  # the checkout's own installer stages the ApplicationPlugins bundle
    included, missing = install.stage_bundle(stage_dir / "bundle")
    if missing:
        sys.exit(f"bridges missing from native/bin for {missing}")
    print(f"staged runtime, docs and bridges for {included}")


def check_runtime(stage_dir: Path, v: str) -> None:
    python = stage_dir / "app" / "runtime" / "python.exe"
    out = subprocess.run([str(python), "-c",
                          "import importlib.metadata as m, maxmcp.server, maxmcp.opencode_config, tomlkit;"
                          "print(m.version('3dsmax-mcp'))"],
                         capture_output=True, text=True, cwd=stage_dir)
    if out.returncode or out.stdout.strip() != v:
        sys.exit(f"staged runtime check failed: {out.stdout}{out.stderr}")
    print(f"staged runtime imports maxmcp {out.stdout.strip()}")


def compile_setup(iscc: Path, stage_dir: Path, v: str) -> Path:
    subprocess.run([str(iscc), f"/DAppVersion={display_version(v)}", f"/DStageDir={stage_dir}",
                    str(HERE / "3dsmax-mcp.iss")], cwd=HERE, check=True)
    exe = HERE / "output" / f"3dsmax-mcp-{display_version(v)}-Setup.exe"
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    (exe.parent / (exe.name + ".sha256")).write_text(f"{digest}  {exe.name}\n", encoding="ascii")
    print(f"{exe} sha256 {digest}")
    return exe


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-runtime", type=Path, default=DEFAULT_BASE)
    ap.add_argument("--iscc", type=Path, default=None)
    ap.add_argument("--dist", type=Path, default=ROOT / "dist")
    ap.add_argument("--skip-build", action="store_true", help="use the wheel already in --dist")
    args = ap.parse_args()
    v = version()
    iscc = args.iscc or next((p for p in DEFAULT_ISCC if p.exists()), None)
    if not iscc or not iscc.exists():
        sys.exit("ISCC.exe (Inno Setup 6) not found; pass --iscc")
    wheel = (sorted(args.dist.glob(f"3dsmax_mcp-{v}-*.whl"))[-1] if args.skip_build else build_dists(args.dist))
    stage_dir = HERE / "stage"
    stage(args.base_runtime, wheel, stage_dir)
    check_runtime(stage_dir, v)
    compile_setup(iscc, stage_dir, v)


if __name__ == "__main__":
    main()
