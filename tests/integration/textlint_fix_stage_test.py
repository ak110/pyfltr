"""textlintのfix段と通常の段の組合せを、公開入口の`pyfltr run`から観測する結合試験。

fail-fastでない実行はfix段の修正後の検査（Step2）を省き、fix段の失敗を通常の段の結果へ統合する。
修正と検査の結果の組合せごとに、最終の成否・終了コード・診断と、textlintの起動回数を確かめる。
外部ツールの実行ファイルだけを、`mode.json`の指定どおりに振る舞うPythonのスタブへ差し替える。
"""

import json
import pathlib
import subprocess
import sys

import pytest

# `--fix`付きの起動を修正、それ以外を検査として扱う。起動ごとに引数を`argv.jsonl`へ記録する。
# 修正: `fix_rc`で終了し、`fix_changes`が真なら文書を書き換える。
# 検査: `lint_violation`が真なら違反1件をJSONで出力して終了1、偽なら空の結果で終了0とする。
_TEXTLINT_SCRIPT = (
    "import json, pathlib, sys; "
    "mode = json.loads(pathlib.Path('mode.json').read_text(encoding='utf-8')); "
    "pathlib.Path('argv.jsonl').open('a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n'); "
    "doc = pathlib.Path('docs/a.md'); "
    "fixing = '--fix' in sys.argv\n"
    "if fixing:\n"
    "    if mode['fix_changes']:\n"
    "        doc.write_text('# fixed\\n', encoding='utf-8')\n"
    "    sys.exit(mode['fix_rc'])\n"
    "msg = {'type': 'lint', 'ruleId': 'sample', 'message': 'violation', 'line': 1, 'column': 1, "
    "'loc': {'start': {'line': 1, 'column': 1}, 'end': {'line': 1, 'column': 2}}, 'severity': 2}\n"
    "print(json.dumps([{'messages': [msg] if mode['lint_violation'] else [], 'filePath': str(doc.resolve())}]))\n"
    "sys.exit(1 if mode['lint_violation'] else 0)\n"
)


def _run(tmp_path: pathlib.Path, mode: dict, *extra: str) -> tuple[dict, list[list[str]]]:
    settings = f"""
[tool.pyfltr]
respect-gitignore = false
textlint = true
textlint-runner = "direct"
textlint-path = {json.dumps(sys.executable)}
textlint-args = {json.dumps(["-c", _TEXTLINT_SCRIPT])}
"""
    (tmp_path / "pyproject.toml").write_text(settings, encoding="utf-8")
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs/a.md").write_text("# a\n", encoding="utf-8")
    (tmp_path / "mode.json").write_text(json.dumps(mode), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyfltr",
            "run",
            "--work-dir",
            str(tmp_path),
            "--commands=textlint",
            "--no-quiet",
            "--no-cache",
            "--output-format=jsonl",
            *extra,
            "docs/a.md",
        ],
        capture_output=True,
        encoding="utf-8",
        errors="backslashreplace",
        check=False,
        timeout=60,
        cwd=tmp_path,
    )
    records = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    command = next(
        record
        for record in records
        if record.get("kind") == "command" and record.get("command") == "textlint" and record.get("status") != "running"
    )
    diagnostics = [record for record in records if record.get("kind") == "diagnostic"]
    log = tmp_path / "argv.jsonl"
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return command | {"exit": proc.returncode, "diagnostic_records": diagnostics}, calls


@pytest.mark.parametrize(
    "mode,status,diagnostics",
    [
        # 修正が非0で通常の検査が違反なし: 修正処理の失敗としてfailedを保つ。
        ({"fix_rc": 1, "fix_changes": False, "lint_violation": False}, "failed", 0),
        # 修正が非0で通常の検査に違反あり: 通常の段の違反を診断とする。
        ({"fix_rc": 1, "fix_changes": False, "lint_violation": True}, "failed", 1),
        # 修正が0で内容に変化あり: 修正後の通常の段が違反なしならsucceeded。
        ({"fix_rc": 0, "fix_changes": True, "lint_violation": False}, "succeeded", 0),
        # 修正が0で変化なし。
        ({"fix_rc": 0, "fix_changes": False, "lint_violation": False}, "succeeded", 0),
        # 修正が致命的な終了コード。
        ({"fix_rc": 2, "fix_changes": False, "lint_violation": False}, "failed", 0),
    ],
)
def test_fix_stage_combination_without_fail_fast(tmp_path: pathlib.Path, mode: dict, status: str, diagnostics: int) -> None:
    """fail-fastでない実行は、修正1回と通常の段の検査1回だけを起動し、組合せごとの成否を保つ。"""
    result, calls = _run(tmp_path, mode)
    assert result["status"] == status
    assert result["exit"] == (1 if status == "failed" else 0)
    assert result["diagnostics"] == diagnostics
    assert [("--fix" in call) for call in calls] == [True, False]


def test_fix_failure_with_fail_fast_reports_residual_violation(tmp_path: pathlib.Path) -> None:
    """fail-fastで修正が非0の場合は通常の段へ進まないため、fix段の中で検査して残存違反を報告する。"""
    result, calls = _run(tmp_path, {"fix_rc": 1, "fix_changes": False, "lint_violation": True}, "--fail-fast")
    assert result["status"] == "failed"
    assert result["exit"] == 1
    assert result["diagnostics"] == 1
    assert [("--fix" in call) for call in calls] == [True, False]
