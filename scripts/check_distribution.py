"""公開候補のwheelとsource distributionを独立環境へ導入して検査する。"""

import email.parser
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import zipfile


def _run(command: list[str], *, cwd: pathlib.Path, env: dict[str, str]) -> None:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=300,
        check=False,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"配布物検査の工程に失敗しました（終了コード {result.returncode}）: {command[0]}")


def main() -> int:
    """指定候補または一時ビルドした配布物を導入し、公開入口を確認する。"""
    root = pathlib.Path(__file__).resolve().parents[1]
    uv = shutil.which("uv")
    if uv is None:
        sys.stderr.write("配布物検査にはuvが必要です。uvを導入して再実行してください。\n")
        return 1
    candidate = os.environ.get("PYFLTR_DISTRIBUTION_DIR")
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
            "UV_PROJECT_ENVIRONMENT",
            "UV_PROJECT",
            "UV_WORKING_DIR",
            "UV_FROZEN",
            "AI_AGENT",
            "CODEX_CI",
            "CLAUDECODE",
            "CURSOR_AGENT",
            "PYFLTR_OUTPUT_FORMAT",
        }
    }
    try:
        with tempfile.TemporaryDirectory(prefix="pyfltr-distribution-") as temporary:
            work = pathlib.Path(temporary)
            dist = pathlib.Path(candidate).resolve() if candidate else work / "dist"
            if candidate is None:
                _run(
                    [uv, "build", "--no-config", "--no-sources", "--exclude-newer", "1 day", "--out-dir", str(dist), str(root)],
                    cwd=work,
                    env=env,
                )
            wheels = list(dist.glob("pyfltr-*.whl"))
            sources = list(dist.glob("pyfltr-*.tar.gz"))
            if len(wheels) != 1 or len(sources) != 1:
                raise RuntimeError(
                    f"pyfltrのwheelとsource distributionを各1件用意してください: {dist}"
                    f"（wheel {len(wheels)}件、source distribution {len(sources)}件）"
                )
            wheel = wheels[0]
            with zipfile.ZipFile(wheel) as archive:
                metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
                metadata = email.parser.BytesParser().parsebytes(archive.read(metadata_name))
            version = metadata["Version"]
            for index, artifact in enumerate([wheel, *sources]):
                venv = work / f"venv-{index}"
                _run([uv, "venv", "--no-config", "--python", sys.executable, str(venv)], cwd=work, env=env)
                python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                _run(
                    [uv, "pip", "install", "--no-config", "--python", str(python), "--exclude-newer", "1 day", str(artifact)],
                    cwd=work,
                    env=env,
                )
                probe_env = dict(env)
                probe_env["PYFLTR_CACHE_DIR"] = str(work / f"cache-{index}")
                probe_env["PYFLTR_GLOBAL_CONFIG"] = str(work / "global.toml")
                probe_env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
                _run([str(python), "-I", str(root / "scripts/distribution_probe.py"), version], cwd=work, env=probe_env)
                print(f"配布物検査成功: {artifact.name}")
        return 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"エラー: {exc}。ビルド・導入・公開入口の出力を確認してください。公開は行っていません。\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
