"""対応ツール群を実起動して終了確認するスモークテスト。

`pnpm 11`での`enableGlobalVirtualStore`既定変更のような外部ツール側の挙動変化が、
pyfltrの起動経路を破壊していないことをCI上で早期検出する目的のテスト群である。
コマンドライン組立だけを検証する既存テストと異なり、実バイナリを起動してJSONL出力の
ステータス（`skipped`以外）を確認する。

ローカル実行時、対象ツールが未インストールの場合は当該ケースをスキップする。
CI実行時（環境変数`CI`が設定されているとき）は失敗扱いとし、ツール群の同梱抜けを検知する。

除外ツール（理由付き）:
    - `cargo-fmt` / `cargo-clippy` / `cargo-check` / `cargo-test` / `cargo-deny`:
      Rustツールチェインは公式pyfltrイメージにも未同梱で実行が重い。
    - `dotnet-format` / `dotnet-build` / `dotnet-test`:
      .NETツールチェインは公式pyfltrイメージにも未同梱で実行が重い。
    - `glab-ci-lint`:
      GitLab APIへのネットワーク・認証アクセスが必須でCI上の安定実行が困難。
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest


@dataclasses.dataclass(frozen=True)
class _Case:
    """1ツール分のスモーク起動仕様。"""

    tool: str
    """pyfltrコマンド名。"""

    workspace: str
    """`tests/smoke_data/`配下のサブディレクトリ名。pyfltrのcwdに採用する。"""

    targets: tuple[str, ...]
    """workspaceからの相対パスでpyfltrへ渡すターゲット。"""

    required_bins: tuple[str, ...] = ()
    """ローカル実行時に存在を要求するバイナリ名。
    すべてが見つかった場合のみ実行する。CI実行時は要求の有無に関わらず実行する。
    """

    git_required: bool = False
    """テスト実行前に当該workspaceで`git init`が必要か否か。
    pre-commitやgitleaksのようにgit管理を前提とするツール向け。
    """


_CASES: tuple[_Case, ...] = (
    # Python系: 本体依存に同梱されているので実環境の追加バイナリ要求なし
    _Case("ruff-format", "basic", ("sample.py",)),
    _Case("ruff-check", "basic", ("sample.py",)),
    _Case("pylint", "basic", ("sample.py",)),
    _Case("mypy", "basic", ("sample.py",)),
    _Case("pyright", "basic", ("sample.py",)),
    _Case("ty", "basic", ("sample.py",)),
    _Case("pytest", "basic", ("sample_test.py",)),
    _Case("uv-sort", "uv_sort_workspace", ("pyproject.toml",)),
    # JS/TS系: pnpm経路で解決
    _Case("prettier", "ts_workspace", ("sample.ts",), required_bins=("pnpm",)),
    _Case("tsc", "ts_workspace", ("sample.ts",), required_bins=("pnpm",)),
    _Case("eslint", "ts_workspace", ("sample.ts",), required_bins=("pnpm",)),
    _Case("biome", "ts_workspace", ("sample.ts",), required_bins=("pnpm",)),
    _Case("oxlint", "ts_workspace", ("sample.ts",), required_bins=("pnpm",)),
    _Case("vitest", "ts_workspace", ("sample.test.ts",), required_bins=("pnpm",)),
    # ドキュメント系: pnpm経路
    _Case("markdownlint", "basic", ("sample.md",), required_bins=("pnpm",)),
    _Case("textlint", "textlint_workspace", ("sample.md",), required_bins=("pnpm",)),
    # mise経由のネイティブバイナリ
    _Case("shfmt", "basic", ("sample.sh",), required_bins=("mise",)),
    _Case("taplo", "basic", ("sample.toml",), required_bins=("mise",)),
    _Case("actionlint", "basic", (".github/workflows/ci.yaml",), required_bins=("mise",)),
    _Case("ec", "basic", ("sample.txt",), required_bins=("mise",)),
    _Case("hadolint", "basic", ("Dockerfile",), required_bins=("mise",)),
    _Case("gitleaks", "basic", ("sample.txt",), required_bins=("mise",), git_required=True),
    # 独立バイナリ
    _Case("shellcheck", "basic", ("sample.sh",), required_bins=("shellcheck",)),
    _Case("yamllint", "basic", ("sample.yaml",), required_bins=("yamllint",)),
    _Case("typos", "basic", ("sample.txt",), required_bins=("typos",)),
    _Case("pre-commit", "pre_commit_workspace", ("sample.py",), required_bins=("pre-commit",), git_required=True),
)

_REPO_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[1]
_SMOKE_DATA_ROOT: pathlib.Path = _REPO_ROOT / "tests" / "smoke_data"

# ツールが起動して正常に完了したことを確認するための許容ステータス。
# `skipped`/`resolution_failed`/`failed`/`warning`はツール起動自体が破綻しているか、
# サンプル側の問題で診断が出た状態を表す。pnpm 11のような起動経路の破壊と
# 通常失敗を区別するため、起動成立かつ診断ゼロの`succeeded`/`formatted`のみを通す。
_OK_STATUSES: frozenset[str] = frozenset({"succeeded", "formatted"})


def _is_ci() -> bool:
    """CI環境変数が設定されている（CI実行と判定される）か。"""
    return bool(os.environ.get("CI"))


def _ensure_required_bins(case: _Case) -> None:
    """必要バイナリが揃っているか確認する。

    CI実行時は揃っていない＝環境構築不備として失敗させる。
    ローカル実行時は当該ケースをスキップする。
    """
    missing = [name for name in case.required_bins if shutil.which(name) is None]
    if not missing:
        return
    message = f"{case.tool}: required binaries missing: {', '.join(missing)}"
    if _is_ci():
        pytest.fail(message)
    pytest.skip(message)


def _prepare_workspace(case: _Case, tmp_path: pathlib.Path) -> pathlib.Path:
    """smoke_data配下のworkspaceをtmp_pathへコピーし必要なら`git init`を施す。"""
    src = _SMOKE_DATA_ROOT / case.workspace
    dst = tmp_path / case.workspace
    shutil.copytree(src, dst)
    if case.git_required:
        # gitleaks/pre-commit はリポジトリ管理を前提とするためinit + 全ファイル登録まで実施する。
        subprocess.run(["git", "init", "--quiet"], cwd=dst, check=True)
        subprocess.run(["git", "add", "--all"], cwd=dst, check=True)
        subprocess.run(
            ["git", "-c", "user.email=smoke@example.com", "-c", "user.name=smoke", "commit", "--quiet", "-m", "smoke"],
            cwd=dst,
            check=True,
        )
    return dst


def _run_pyfltr(workspace: pathlib.Path, command: str, targets: tuple[str, ...]) -> list[dict]:
    """pyfltr CLIをsubprocess起動し、JSONL出力をパースして返す。"""
    cmd = [
        sys.executable,
        "-m",
        "pyfltr",
        "run",
        f"--commands={command}",
        "--no-archive",
        "--no-cache",
        "--no-clear",
        "--output-format=jsonl",
        *targets,
    ]
    proc = subprocess.run(
        cmd,
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    records: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # text混入があればスキップする（pyfltr側のバグ検出時は他assertで失敗させる）。
            continue
    if not records:
        pytest.fail(f"pyfltr produced no JSONL output. stderr=\n{proc.stderr}\nstdout=\n{proc.stdout}")
    return records


def _extract_command_record(records: list[dict], command: str) -> dict | None:
    """JSONLレコード列から指定コマンドのcommandレコードを取得する。"""
    for record in records:
        if record.get("kind") == "command" and record.get("command") == command:
            return record
    return None


@pytest.mark.smoke
@pytest.mark.parametrize("case", _CASES, ids=lambda c: c.tool)
def test_tool_smoke(case: _Case, tmp_path: pathlib.Path) -> None:
    """対応ツールがpyfltr経由で実起動し、起動成立を示すステータスを返す。"""
    _ensure_required_bins(case)
    workspace = _prepare_workspace(case, tmp_path)
    records = _run_pyfltr(workspace, case.tool, case.targets)
    record = _extract_command_record(records, case.tool)
    assert record is not None, f"{case.tool}: command record not found in JSONL output: {records}"
    status = record.get("status")
    assert status in _OK_STATUSES, f"{case.tool}: unexpected status={status!r} record={record}"
