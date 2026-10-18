"""公開候補のwheelとsource distributionを独立環境へ導入して検査する。"""

import email.parser
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import zipfile

import pyfltr.command.markdownlint_env

_BRACES_PROBE = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const fromCli = createRequire(fs.realpathSync(path.join(process.argv[1], 'node_modules/markdownlint-cli2/package.json')));
const fromMicromatch = createRequire(fromCli.resolve('micromatch'));
const actual = fromMicromatch.resolve('braces');
const braces = fromMicromatch('braces');
const lib = path.join(path.dirname(actual), 'lib');
for (const name of ['compile', 'expand', 'stringify', 'utils']) {
  const source = fs.readFileSync(path.join(lib, name + '.js'), 'utf8');
  assert.match(source, /while \(.*\.length\)/);
}
assert.deepEqual(braces.expand('x{a,b}'), ['xa', 'xb']);
assert.deepEqual(braces.expand('a{1..3}'), ['a1', 'a2', 'a3']);
assert.deepEqual(braces.expand('{a,{b,c}}'), ['a', 'b', 'c']);
assert.equal(braces.compile(braces.parse('x{a,b}')), 'x(a|b)');
assert.equal(braces.stringify(braces.parse('x{a,b}')), 'x{a,b}');
assert.deepEqual(braces.expand({type: 'root', nodes: [{type: 'text', value: ['a', ['b']]}]}), ['a', 'b']);
const utils = require(path.join(lib, 'utils.js'));
assert.deepEqual(utils.flatten(['a', [undefined, ['b']], 'c']), ['a', 'b', 'c']);
console.log(JSON.stringify({braces: actual, publicApis: 'compile,expand,stringify,flatten'}));
"""


def verify_markdownlint_environment(workspace: pathlib.Path) -> None:
    """通常起動が選んだbracesの実体と浅い公開API・AST入力を検査する。"""
    environment = pyfltr.command.markdownlint_env.resolve_environment(workspace)
    _run(["node", "-e", _BRACES_PROBE, str(environment.root)], cwd=workspace, env=dict(os.environ))


def _run(command: list[str], *, cwd: pathlib.Path, env: dict[str, str], expected: int = 0) -> None:
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
    if result.returncode != expected:
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
                _run(
                    [str(python), "-I", str(root / "scripts/check_distribution.py"), "--markdownlint-probe"],
                    cwd=work,
                    env=probe_env,
                )
                print(f"配布物検査成功: {artifact.name}")
        return 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"エラー: {exc}。ビルド・導入・公開入口の出力を確認してください。公開は行っていません。\n")
        return 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--markdownlint-probe"]:
        probe_workspace = pathlib.Path.cwd()
        (probe_workspace / "sample.md").write_text("# Sample\n\nText   \n", encoding="utf-8")
        (probe_workspace / ".npmrc").write_text("minimum-release-age=1440\n", encoding="utf-8")
        invocation = [
            sys.executable,
            "-I",
            "-m",
            "pyfltr",
            "run",
            "sample.md",
            "--commands=markdownlint",
            "--enable=markdownlint",
            "--no-archive",
            "--no-cache",
            "--output-format=jsonl",
            "--no-quiet",
        ]
        _run([*invocation, "--no-fix"], cwd=probe_workspace, env=dict(os.environ), expected=1)
        _run([*invocation, "--exit-zero-even-if-formatted"], cwd=probe_workspace, env=dict(os.environ))
        assert (probe_workspace / "sample.md").read_text(encoding="utf-8") == "# Sample\n\nText\n"
        verify_markdownlint_environment(probe_workspace)
    else:
        sys.exit(main())
