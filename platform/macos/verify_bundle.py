#!/usr/bin/env python3
"""Run the frozen app after relocation, without developer PATH/Python/DYLD settings."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def verify(app: Path) -> dict:
    if sys.platform != "darwin":
        raise RuntimeError("Bundle verification requires macOS")
    app = app.resolve(strict=True)
    if app.suffix != ".app":
        raise ValueError("Expected a .app bundle")
    with tempfile.TemporaryDirectory(prefix="nso bundle check ") as tmp:
        root = Path(tmp)
        relocated = root / "Applications With Spaces" / app.name
        relocated.parent.mkdir()
        shutil.copytree(app, relocated, symlinks=True)
        home = root / "home"
        home.mkdir()
        report = root / "diagnostics.json"
        executable = relocated / "Contents/MacOS/NSO-GameCube-Controller-Pairing-App"
        proc = subprocess.run(
            [str(executable), "--bundle-self-test", "--report", str(report)],
            cwd=root,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(home),
                 "TMPDIR": str(root), "LANG": "en_US.UTF-8", "PYTHONNOUSERSITE": "1"},
            capture_output=True, text=True, timeout=90,
        )
        if not report.is_file():
            raise RuntimeError(f"App exited {proc.returncode} without a report: {proc.stderr}")
        result = json.loads(report.read_text(encoding="utf-8"))
        if proc.returncode != 0 or result.get("ok") is not True:
            raise RuntimeError(json.dumps(result, indent=2))
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.app), indent=2))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Bundle verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
