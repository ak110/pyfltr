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
    _Case("arid", "basic", ("sample.py",)),
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
    _Case("pinact", "pinact_workspace", (".github/workflows/pinned.yaml",), required_bins=("mise",)),
    _Case("ec", "basic", ("sample.txt",), required_bins=("mise",)),
    _Case("hadolint", "basic", ("Dockerfile",), required_bins=("mise",)),
    _Case("gitleaks", "basic", ("sample.txt",), required_bins=("mise",), git_required=True),
    # 独立バイナリ
    _Case("shellcheck", "basic", ("sample.sh",), required_bins=("shellcheck",)),
    _Case("yamllint", "basic", ("sample.yaml",), required_bins=("yamllint",)),
    _Case("typos", "basic", ("sample.txt",), required_bins=("typos",)),
    _Case("pre-commit", "pre_commit_workspace", ("sample.py",), required_bins=("pre-commit",), git_required=True),
    _Case("prek", "prek_workspace", ("sample.py",), required_bins=("prek",), git_required=True),
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


def _run_pyfltr(
    workspace: pathlib.Path, command: str, targets: tuple[str, ...], env: dict[str, str] | None = None
) -> list[dict]:
    """pyfltr CLIをsubprocess起動し、JSONL出力をパースして返す。

    `env`を省略した場合は現在の環境変数を引き継ぐ。
    """
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
        env=env,
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
    """JSONLレコード列から指定コマンドの最後のcommandレコードを取得する。"""
    for record in reversed(records):
        if record.get("kind") == "command" and record.get("command") == command:
            return record
    return None


def test_extract_command_record_returns_last_matching_record() -> None:
    """heartbeatより後の終端レコードをコマンドの確定結果として返す。"""
    records = [
        {"kind": "command", "command": "other", "status": "succeeded"},
        {"kind": "command", "command": "target", "status": "running"},
        {"kind": "command", "command": "target", "status": "succeeded"},
    ]

    assert _extract_command_record(records, "target") == records[-1]


def test_extract_command_record_returns_none_without_matching_record() -> None:
    """指定コマンドのレコードが無い場合はNoneを返す。"""
    records = [{"kind": "command", "command": "other", "status": "succeeded"}]

    assert _extract_command_record(records, "target") is None


@pytest.mark.smoke
# pytest全体の既定は`--timeout=60`だが、`_run_pyfltr`内部の`subprocess.run`は180秒を許容する。
# 外側が先に発火すると内部タイムアウトの明確なエラーが失われるため、外側を内側より長く取る。
@pytest.mark.timeout(300)
@pytest.mark.usefixtures("_disable_faulthandler_timeout")
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


def _env_without_github_token() -> dict[str, str]:
    """GitHub APIの認証情報を除いた環境変数を返す。"""
    return {key: value for key, value in os.environ.items() if key not in ("GITHUB_TOKEN", "GH_TOKEN")}


@pytest.mark.smoke
@pytest.mark.timeout(300)
@pytest.mark.usefixtures("_disable_faulthandler_timeout")
def test_pinact_passes_pinned_workflow_without_token(tmp_path: pathlib.Path) -> None:
    """ピン留め済みのworkflowは、認証情報なしでも成功し、ファイルを書き換えない。"""
    case = _Case("pinact", "pinact_workspace", (".github/workflows/pinned.yaml",), required_bins=("mise",))
    _ensure_required_bins(case)
    workspace = _prepare_workspace(case, tmp_path)
    target = workspace / ".github" / "workflows" / "pinned.yaml"
    before = target.read_bytes()
    records = _run_pyfltr(workspace, case.tool, case.targets, env=_env_without_github_token())
    record = _extract_command_record(records, "pinact")
    assert record is not None, records
    assert record.get("status") == "succeeded", record
    assert target.read_bytes() == before


@pytest.mark.smoke
@pytest.mark.timeout(300)
@pytest.mark.usefixtures("_disable_faulthandler_timeout")
def test_pinact_reports_unpinned_actions(tmp_path: pathlib.Path) -> None:
    """タグやブランチで書いた`uses:`を、ファイルと行番号付きの診断として報告して失敗する。"""
    case = _Case("pinact", "pinact_workspace", (".github/workflows/unpinned.yaml",), required_bins=("mise",))
    _ensure_required_bins(case)
    workspace = _prepare_workspace(case, tmp_path)
    target = workspace / ".github" / "workflows" / "unpinned.yaml"
    before = target.read_bytes()
    records = _run_pyfltr(workspace, case.tool, case.targets, env=_env_without_github_token())
    record = _extract_command_record(records, "pinact")
    assert record is not None, records
    assert record.get("status") == "failed", record
    locations = sorted(
        (diagnostic["file"], message["line"])
        for diagnostic in records
        if diagnostic.get("kind") == "diagnostic" and diagnostic.get("command") == "pinact"
        for message in diagnostic["messages"]
    )
    assert locations == [(".github/workflows/unpinned.yaml", 7), (".github/workflows/unpinned.yaml", 8)]
    assert target.read_bytes() == before


# 違反3件（error 2件・warning 1件）を生む入力。ルールを明示列挙し、yamllintの既定ルールの変更に依存させない。
_YAMLLINT_CONFIG = "rules:\n  colons: enable\n  commas: enable\n  truthy:\n    level: warning\n"
_YAMLLINT_BAD_YAML = "a:  1\nb: [1,2]\nc: yes\n"
_YAMLLINT_EXPECTED_MESSAGES = [
    (1, 4, "colons", "error"),
    (2, 7, "commas", "error"),
    (3, 4, "truthy", "warning"),
]


def _prepare_yamllint_workspace(tmp_path: pathlib.Path, *, config_via_args: bool) -> pathlib.Path:
    """yamllintの違反を含む作業ディレクトリを用意する。

    `config_via_args`が真なら自動探索されない名前で設定ファイルを置き、`yamllint-args`の`-c`で渡す。
    偽なら自動探索される`.yamllint.yml`へ置き、`yamllint-args`を指定しない。
    """
    workspace = tmp_path / "yamllint_workspace"
    workspace.mkdir()
    pyproject = "[tool.pyfltr]\nyamllint = true\nrespect-gitignore = false\n"
    if config_via_args:
        (workspace / "custom-yamllint.yml").write_text(_YAMLLINT_CONFIG, encoding="utf-8")
        pyproject += 'yamllint-args = ["-c", "custom-yamllint.yml"]\n'
    else:
        (workspace / ".yamllint.yml").write_text(_YAMLLINT_CONFIG, encoding="utf-8")
    (workspace / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    (workspace / "bad.yaml").write_text(_YAMLLINT_BAD_YAML, encoding="utf-8")
    return workspace


@pytest.mark.smoke
@pytest.mark.timeout(300)
@pytest.mark.usefixtures("_disable_faulthandler_timeout")
@pytest.mark.parametrize("config_via_args", [False, True], ids=["default-args", "yamllint-args"])
def test_yamllint_reports_violations_as_diagnostics(tmp_path: pathlib.Path, config_via_args: bool) -> None:
    """yamllintの違反を位置・ルール・重大度付きの診断として報告する。

    yamllintの既定出力はGitHub Actions上で`::error`形式へ切り替わるため、同環境の変数を設定しても
    同じ診断になることを併せて確認する。`yamllint-args`を上書きしても出力形式の注入は保たれる。
    """
    _ensure_required_bins(_Case("yamllint", "yamllint_workspace", ("bad.yaml",), required_bins=("yamllint",)))
    workspace = _prepare_yamllint_workspace(tmp_path, config_via_args=config_via_args)
    env = {**os.environ, "GITHUB_ACTIONS": "true", "GITHUB_WORKFLOW": "ci"}
    records = _run_pyfltr(workspace, "yamllint", ("bad.yaml",), env=env)
    record = _extract_command_record(records, "yamllint")
    assert record is not None, records
    assert record.get("status") == "failed", record
    assert record.get("diagnostics") == len(_YAMLLINT_EXPECTED_MESSAGES), record
    messages = sorted(
        (diagnostic["file"], message["line"], message["col"], message["rule"], message["severity"])
        for diagnostic in records
        if diagnostic.get("kind") == "diagnostic" and diagnostic.get("command") == "yamllint"
        for message in diagnostic["messages"]
    )
    assert messages == [("bad.yaml", *expected) for expected in _YAMLLINT_EXPECTED_MESSAGES]
