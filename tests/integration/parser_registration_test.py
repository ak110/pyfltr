import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.tools

# パーサーを持たない組み込みlinter・testerのうち、既知のもの。
# いずれも失敗時の診断が0件となる既存の欠陥であり、パーサーの追加には各ツールの出力形式の調査と
# 実測（cargo系・dotnet系はRust・.NETツールチェーン）を個別に要するため未対応のまま登録している。
# パーサーを追加した場合は本集合から削除する。
_COMMANDS_WITHOUT_PARSER_KNOWN: frozenset[str] = frozenset(
    {
        "hadolint",
        "gitleaks",
        "tsc",
        "oxlint",
        "cargo-clippy",
        "cargo-check",
        "cargo-deny",
        "dotnet-build",
        "cargo-test",
        "dotnet-test",
    }
)


def _commands_without_parser(commands: dict[str, pyfltr.tools.CommandInfo]) -> set[str]:
    """診断を抽出する手段を持たないlinter・tester型コマンドの集合を返す。

    formatterは2段階実行の経路で成否を扱うため対象から外す。
    """
    registered = {
        name
        for name, info in pyfltr.tools.BUILTIN_COMMANDS.items()
        if info.diagnostic_pattern is not None or info.parser is not None or info.path_base_parser is not None
    }
    return {
        name
        for name, info in commands.items()
        if info.type in ("linter", "tester") and info.error_pattern is None and name not in registered
    }


def test_builtin_linters_and_testers_have_parser() -> None:
    """組み込みlinter・testerはパーサーを持つか、既知の未対応として登録されている。

    パーサーを登録しない組み込みコマンドは、失敗しても診断が常に0件となる。
    `error-parser-reviewer`は`error_parser.py`を変更しない追加では起動しないため、本テストで検出する。
    """
    actual = _commands_without_parser(pyfltr.tools.BUILTIN_COMMANDS)
    assert actual == _COMMANDS_WITHOUT_PARSER_KNOWN, (
        "パーサー未登録の組み込みlinter・testerが既知の集合と一致しない。"
        "新規追加分は`pyfltr/parsing/entry.py`へパーサーを登録するか、理由を添えて"
        "`_COMMANDS_WITHOUT_PARSER_KNOWN`へ加える。パーサーを追加した既知分は同集合から削除する。"
        f" 追加: {sorted(actual - _COMMANDS_WITHOUT_PARSER_KNOWN)}"
        f" 削除: {sorted(_COMMANDS_WITHOUT_PARSER_KNOWN - actual)}"
    )


def test_commands_without_parser_detects_unregistered_linter() -> None:
    """パーサー未登録のlinter・testerを検出し、formatterと`error_pattern`指定を対象外とする。"""
    commands = {
        **pyfltr.tools.BUILTIN_COMMANDS,
        "dummy-linter": pyfltr.tools.CommandInfo(type="linter"),
        "dummy-tester": pyfltr.tools.CommandInfo(type="tester"),
        "dummy-formatter": pyfltr.tools.CommandInfo(type="formatter"),
        "dummy-pattern": pyfltr.tools.CommandInfo(type="linter", error_pattern=r"(?P<file>.+)"),
    }

    actual = _commands_without_parser(commands)

    assert actual - _COMMANDS_WITHOUT_PARSER_KNOWN == {"dummy-linter", "dummy-tester"}
