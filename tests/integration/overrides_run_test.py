"""`overrides`と`env-file`を、公開入口の`pyfltr run`から観測する結合試験。

設定の読込（`tests/config/overrides_test.py`）に加え、実際の実行で対象ファイル・追加引数・警告・
キャッシュの利用が環境変数に従って切り替わることを確かめる。
外部ツールの実行ファイルだけを、受け取った引数を記録するPythonのスタブへ差し替える。
"""

import json
import os
import pathlib
import subprocess
import sys

import pytest

# 受け取った引数を`argv.jsonl`へ1行ずつ追記して正常終了するスタブ。
_RECORD_SCRIPT = (
    "import json, pathlib, sys; pathlib.Path('argv.jsonl').open('a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')"
)

_BASE_SETTINGS = f"""
[tool.pyfltr]
respect-gitignore = false
env-file = ".env"
ec = true
ec-runner = "direct"
ec-path = {json.dumps(sys.executable)}
ec-args = {json.dumps(["-c", _RECORD_SCRIPT])}
ec-targets = ["*.py"]
"""

_GPU_OVERRIDE = """
[[tool.pyfltr.overrides]]
when = "not RUN_GPU_TESTS"
extend-exclude = ["tests/gpu"]
ec-extend-args = ["--not-gpu"]
"""


def _project(tmp_path: pathlib.Path, settings: str) -> pathlib.Path:
    for name in ("tests/cpu_test.py", "tests/gpu/gpu_test.py", "tests/slow/slow_test.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(settings, encoding="utf-8")
    return tmp_path


def _run(
    tmp_path: pathlib.Path, env: dict[str, str], *extra: str, commands: str = "ec"
) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    proc_env = {key: value for key, value in os.environ.items() if key not in ("RUN_GPU_TESTS", "RUN_SLOW_TESTS", "STRICT")}
    proc_env.update(env)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyfltr",
            "run",
            "--work-dir",
            str(tmp_path),
            f"--commands={commands}",
            "--no-quiet",
            "--output-format=jsonl",
            *extra,
            ".",
        ],
        capture_output=True,
        encoding="utf-8",
        errors="backslashreplace",
        check=False,
        timeout=60,
        cwd=tmp_path,
        env=proc_env,
    )
    records = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    return proc, records


def _recorded_argv(tmp_path: pathlib.Path) -> list[list[str]]:
    log = tmp_path / "argv.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def _targets(argv: list[str]) -> set[str]:
    return {pathlib.PurePath(arg).as_posix() for arg in argv if arg.endswith(".py")}


@pytest.mark.parametrize(
    "env,excluded",
    [
        ({}, True),
        ({"RUN_GPU_TESTS": "0"}, True),
        ({"RUN_GPU_TESTS": "false"}, True),
        ({"RUN_GPU_TESTS": "1"}, False),
        ({"RUN_GPU_TESTS": "true"}, False),
    ],
)
def test_run_applies_override_by_process_environment(tmp_path: pathlib.Path, env: dict[str, str], excluded: bool) -> None:
    """未定義・0・falseでは対象から外れ追加引数が渡り、1・trueでは外れず渡らない。"""
    _project(tmp_path, _BASE_SETTINGS + _GPU_OVERRIDE)
    proc, _ = _run(tmp_path, env)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    (argv,) = _recorded_argv(tmp_path)
    assert ("tests/gpu/gpu_test.py" not in _targets(argv)) is excluded
    assert ("--not-gpu" in argv) is excluded
    assert "tests/cpu_test.py" in _targets(argv)


@pytest.mark.parametrize(
    "dotenv,env,excluded",
    [
        ("RUN_GPU_TESTS=1\n", {}, False),
        ("export RUN_GPU_TESTS='true'\n", {"RUN_GPU_TESTS": "false"}, True),
    ],
)
def test_run_reads_env_file_and_prefers_process_value(
    tmp_path: pathlib.Path, dotenv: str, env: dict[str, str], excluded: bool
) -> None:
    """`.env`だけに定義した値で判定し、プロセスの環境変数がある場合はプロセスの値を優先する。"""
    _project(tmp_path, _BASE_SETTINGS + _GPU_OVERRIDE)
    (tmp_path / ".env").write_text(dotenv, encoding="utf-8")
    proc, _ = _run(tmp_path, env)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    (argv,) = _recorded_argv(tmp_path)
    assert ("tests/gpu/gpu_test.py" not in _targets(argv)) is excluded


def _labeled_script(label: str) -> str:
    """受け取った引数の先頭へ`label`を付けて記録するスタブ。置き換えたキーの値がどちらかを判別する。"""
    return (
        "import json, pathlib, sys; "
        f"pathlib.Path('argv.jsonl').open('a', encoding='utf-8').write(json.dumps([{label!r}, *sys.argv[1:]]) + '\\n')"
    )


def test_run_concatenates_extend_keys_and_uses_last_replacement(tmp_path: pathlib.Path) -> None:
    """真の要素が複数あるとき、追加用キーは連結され、置き換えのキーは後の要素の値になる。"""
    settings = (
        _BASE_SETTINGS
        + f"""
[[tool.pyfltr.overrides]]
when = "not RUN_GPU_TESTS"
extend-exclude = ["tests/gpu"]
ec-extend-args = ["--first"]
ec-args = {json.dumps(["-c", _labeled_script("first")])}

[[tool.pyfltr.overrides]]
when = "not RUN_SLOW_TESTS"
extend-exclude = ["tests/slow"]
ec-extend-args = ["--second"]
ec-args = {json.dumps(["-c", _labeled_script("second")])}
"""
    )
    _project(tmp_path, settings)
    proc, _ = _run(tmp_path, {})
    assert proc.returncode == 0, proc.stderr + proc.stdout
    (argv,) = _recorded_argv(tmp_path)
    assert _targets(argv) == {"tests/cpu_test.py"}
    assert argv[0] == "second"
    assert argv.index("--first") < argv.index("--second")


def test_run_warns_and_ignores_invalid_when_element(tmp_path: pathlib.Path) -> None:
    """構文が不正な`when`の要素は警告して無視し、他の要素と通常の設定を適用して実行を続ける。"""
    settings = (
        _BASE_SETTINGS
        + _GPU_OVERRIDE
        + """
[[tool.pyfltr.overrides]]
when = "not (RUN_GPU_TESTS"
extend-exclude = ["tests/cpu_test.py"]
"""
    )
    _project(tmp_path, settings)
    proc, records = _run(tmp_path, {})
    assert proc.returncode == 0, proc.stderr + proc.stdout
    warnings = [record["msg"] for record in records if record.get("kind") == "warning"]
    assert any("`overrides`の2番目の要素" in message for message in warnings)
    (argv,) = _recorded_argv(tmp_path)
    assert _targets(argv) == {"tests/cpu_test.py", "tests/slow/slow_test.py"}
    assert "--not-gpu" in argv


def test_run_without_overrides_keeps_targets_and_args(tmp_path: pathlib.Path) -> None:
    """`overrides`と`env-file`を書かないプロジェクトは、環境変数によらず同じ対象と引数で実行する。"""
    _project(tmp_path, _BASE_SETTINGS.replace('env-file = ".env"\n', ""))
    (tmp_path / ".env").write_text("RUN_GPU_TESTS=0\n", encoding="utf-8")
    first, _ = _run(tmp_path, {})
    second, _ = _run(tmp_path, {"RUN_GPU_TESTS": "1"})
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    argv_first, argv_second = _recorded_argv(tmp_path)
    assert argv_first == argv_second
    assert _targets(argv_first) == {"tests/cpu_test.py", "tests/gpu/gpu_test.py", "tests/slow/slow_test.py"}


# `--strict-marker`を受け取ったときだけ違反1件を報告するtextlintのスタブ。
# textlintはファイルhashキャッシュの対象であり、環境変数で引数を切り替えた再実行がキャッシュと整合するかを観測する。
_TEXTLINT_SCRIPT = (
    "import json, pathlib, sys; "
    "pathlib.Path('argv.jsonl').open('a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n'); "
    "strict = '--strict-marker' in sys.argv; "
    "doc = str(pathlib.Path('docs/a.md').resolve()); "
    "msg = {'type': 'lint', 'ruleId': 'strict', 'message': 'strict violation', 'line': 1, 'column': 1, "
    "'loc': {'start': {'line': 1, 'column': 1}, 'end': {'line': 1, 'column': 2}}, 'severity': 2}; "
    "print(json.dumps([{'messages': [msg] if strict else [], 'filePath': doc}])); "
    "sys.exit(1 if strict else 0)"
)


def test_run_cache_follows_environment_switch(tmp_path: pathlib.Path) -> None:
    """環境変数だけを切り替えた再実行で、キャッシュの利用が切り替え後の実効設定と整合する。"""
    settings = f"""
[tool.pyfltr]
respect-gitignore = false
textlint = true
textlint-runner = "direct"
textlint-path = {json.dumps(sys.executable)}
textlint-args = {json.dumps(["-c", _TEXTLINT_SCRIPT])}

[[tool.pyfltr.overrides]]
when = "STRICT"
textlint-extend-args = ["--strict-marker"]
"""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/a.md").write_text("# a\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(settings, encoding="utf-8")

    def textlint_record(env: dict[str, str]) -> dict:
        proc, records = _run(tmp_path, env, "--no-fix", commands="textlint")
        return next(
            record
            for record in records
            if record.get("kind") == "command" and record.get("command") == "textlint" and record.get("status") != "running"
        ) | {"rc": proc.returncode}

    first = textlint_record({})
    assert first["status"] == "succeeded" and not first.get("cached")
    cached = textlint_record({})
    assert cached["status"] == "succeeded" and cached.get("cached") is True
    strict = textlint_record({"STRICT": "1"})
    assert strict["status"] == "failed" and not strict.get("cached")
    assert strict["rc"] == 1
    again = textlint_record({})
    assert again["status"] == "succeeded" and again.get("cached") is True
