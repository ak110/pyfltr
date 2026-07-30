"""`--commands`指定と有効化状態の警告テスト。"""

import argparse
import json
import pathlib

import pyfltr.cli.pipeline
import pyfltr.config.config


def _make_args(tmp_path: pathlib.Path, *, commands: list[str] | None) -> argparse.Namespace:
    """警告検証用の最小引数を生成する。"""
    target = tmp_path / "sample.md"
    target.write_text("# Title\n", encoding="utf-8")
    return argparse.Namespace(
        output_format="jsonl",
        output_file=None,
        format_source=None,
        stream=False,
        no_clear=True,
        no_ui=True,
        ui=False,
        targets=[target],
        commands=commands,
        exit_zero_even_if_formatted=True,
        include_fix_stage=False,
        fail_fast=False,
        jobs=None,
        no_exclude=False,
        no_gitignore=True,
        allow_external_paths=False,
        human_readable=False,
        shuffle=False,
        verbose=False,
        ci=False,
        only_failed=False,
        from_run=None,
        changed_since=None,
        no_archive=True,
        no_cache=True,
        subcommand="run",
    )


def _run_and_read_jsonl(
    monkeypatch,
    capsys,
    tmp_path: pathlib.Path,
    commands: list[str],
    *,
    args_commands: list[str] | None,
    enabled: set[str] | None = None,
) -> list[dict]:
    """パイプラインを実ツール起動なしで実行し、JSONLレコードを返す。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pyfltr.cli.pipeline.run_commands_with_cli", lambda *args, **kwargs: [])
    config = pyfltr.config.config.create_default_config()
    config.values["respect-gitignore"] = False
    config.values["subproject-use-gitignore"] = False
    for command in enabled or set():
        config.values[command] = True

    pyfltr.cli.pipeline.run_pipeline(_make_args(tmp_path, commands=args_commands), commands, config)

    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def test_unmet_commands_warning_emitted_when_commands_explicit(monkeypatch, capsys, tmp_path) -> None:
    """明示指定された未有効化コマンドはJSONL警告として出力される。"""
    records = _run_and_read_jsonl(
        monkeypatch,
        capsys,
        tmp_path,
        ["textlint"],
        args_commands=["textlint"],
    )

    warnings = [record for record in records if record.get("kind") == "warning"]
    assert warnings == [
        {
            "kind": "warning",
            "source": "commands",
            "msg": "--commandsで指定されたが有効化されていないため未実行のコマンドがあります: textlint",
            "hint": (
                "--enable=textlint または pyproject.toml [tool.pyfltr] で当該コマンドを true に設定して有効化してください。"
            ),
        }
    ]


def test_unmet_commands_warning_not_emitted_without_explicit_commands(monkeypatch, capsys, tmp_path) -> None:
    """`--commands`未指定時は未有効化コマンド警告を出力しない。"""
    records = _run_and_read_jsonl(monkeypatch, capsys, tmp_path, ["textlint"], args_commands=None)

    assert not [record for record in records if record.get("source") == "commands"]


def test_unmet_commands_warning_not_emitted_when_all_enabled(monkeypatch, capsys, tmp_path) -> None:
    """明示指定コマンドがすべて有効な場合は警告を出力しない。"""
    records = _run_and_read_jsonl(
        monkeypatch,
        capsys,
        tmp_path,
        ["textlint"],
        args_commands=["textlint"],
        enabled={"textlint"},
    )

    assert not [record for record in records if record.get("source") == "commands"]


def test_unmet_commands_warning_groups_multiple_commands(monkeypatch, capsys, tmp_path) -> None:
    """複数の未有効化コマンドは1件の警告にまとめる。"""
    records = _run_and_read_jsonl(
        monkeypatch,
        capsys,
        tmp_path,
        ["textlint", "markdownlint", "typos"],
        args_commands=["textlint,markdownlint,typos"],
        enabled={"typos"},
    )

    warning = next(record for record in records if record.get("source") == "commands")
    assert warning["msg"].endswith("textlint, markdownlint")
    assert warning["hint"].startswith("--enable=textlint,markdownlint")


def test_unmet_commands_warning_not_emitted_for_alias_with_any_enabled(monkeypatch, capsys, tmp_path) -> None:
    """エイリアス指定で展開結果に有効化済みが1件以上あれば警告を出力しない。"""
    records = _run_and_read_jsonl(
        monkeypatch,
        capsys,
        tmp_path,
        ["uv-audit", "pnpm-audit", "npm-audit", "yarn-audit"],
        args_commands=["audit"],
        enabled={"pnpm-audit"},
    )

    assert not [record for record in records if record.get("source") == "commands"]


def test_unmet_commands_warning_emitted_for_alias_with_none_enabled(monkeypatch, capsys, tmp_path) -> None:
    """エイリアス指定で展開結果が全て未有効化なら従来どおり警告を出力する。"""
    records = _run_and_read_jsonl(
        monkeypatch,
        capsys,
        tmp_path,
        ["uv-audit", "pnpm-audit", "npm-audit", "yarn-audit"],
        args_commands=["audit"],
    )

    warning = next(record for record in records if record.get("source") == "commands")
    assert "uv-audit" in warning["msg"]
    assert "yarn-audit" in warning["msg"]


def test_unmet_commands_warning_emitted_for_explicit_command_with_alias(monkeypatch, capsys, tmp_path) -> None:
    """エイリアスと個別コマンド名の併記では、個別指定した未有効化コマンドの警告を出力する。"""
    records = _run_and_read_jsonl(
        monkeypatch,
        capsys,
        tmp_path,
        ["uv-audit", "pnpm-audit", "npm-audit", "yarn-audit"],
        args_commands=["audit,uv-audit"],
        enabled={"pnpm-audit"},
    )

    warning = next(record for record in records if record.get("source") == "commands")
    assert warning["msg"].endswith("uv-audit")
