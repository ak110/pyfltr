"""シェル補完生成のテストコード。"""

import pytest

import pyfltr.cli.main
import pyfltr.cli.parser
import pyfltr.cli.shell_completion
import pyfltr.config.config


class TestCollectCompletions:
    """補完データ収集のテスト（generate経由でbashスクリプト内容を検証）。"""

    def test_options_contain_verbose(self):
        parser = pyfltr.cli.parser.build_parser()
        script = pyfltr.cli.shell_completion.generate("bash", parser, frozenset())
        assert "--verbose" in script
        assert "-v" in script

    def test_options_contain_output_format(self):
        parser = pyfltr.cli.parser.build_parser()
        script = pyfltr.cli.shell_completion.generate("bash", parser, frozenset())
        assert "--output-format" in script

    def test_output_format_choices(self):
        parser = pyfltr.cli.parser.build_parser()
        script = pyfltr.cli.shell_completion.generate("bash", parser, frozenset())
        assert "text" in script
        assert "jsonl" in script

    def test_commands_choices_contain_builtin_and_aliases(self):
        parser = pyfltr.cli.parser.build_parser()
        script = pyfltr.cli.shell_completion.generate("bash", parser, frozenset())
        # ビルトインコマンド
        for name in pyfltr.config.config.BUILTIN_COMMAND_NAMES:
            assert name in script
        # 静的エイリアス
        for alias in pyfltr.config.config.DEFAULT_CONFIG["aliases"]:
            assert alias in script


class TestGenerateBash:
    """bash補完スクリプト生成のテスト。"""

    def test_contains_function_and_complete(self):
        parser = pyfltr.cli.parser.build_parser()
        subcommands = frozenset({"ci", "run", "fast", "generate-shell-completion"})
        script = pyfltr.cli.shell_completion.generate("bash", parser, subcommands)
        assert "_pyfltr_completions()" in script
        assert "complete -o default -F _pyfltr_completions pyfltr" in script

    def test_contains_subcommands(self):
        parser = pyfltr.cli.parser.build_parser()
        subcommands = frozenset({"ci", "run", "generate-shell-completion"})
        script = pyfltr.cli.shell_completion.generate("bash", parser, subcommands)
        assert "ci" in script
        assert "run" in script
        assert "generate-shell-completion" in script

    def test_contains_output_format_choices(self):
        parser = pyfltr.cli.parser.build_parser()
        script = pyfltr.cli.shell_completion.generate("bash", parser, frozenset({"ci"}))
        assert "text" in script
        assert "jsonl" in script


class TestGeneratePowershell:
    """PowerShell補完スクリプト生成のテスト。"""

    def test_contains_register_argument_completer(self):
        parser = pyfltr.cli.parser.build_parser()
        subcommands = frozenset({"ci", "run"})
        script = pyfltr.cli.shell_completion.generate("powershell", parser, subcommands)
        assert "Register-ArgumentCompleter -Native -CommandName pyfltr" in script

    def test_contains_subcommands(self):
        parser = pyfltr.cli.parser.build_parser()
        subcommands = frozenset({"ci", "run", "generate-shell-completion"})
        script = pyfltr.cli.shell_completion.generate("powershell", parser, subcommands)
        assert "'ci'" in script
        assert "'run'" in script
        assert "'generate-shell-completion'" in script


class TestMainIntegration:
    """main.py経由の統合テスト。"""

    def test_bash_success(self, capsys):
        rc = pyfltr.cli.main.run(["generate-shell-completion", "bash"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "_pyfltr_completions()" in captured.out

    def test_powershell_success(self, capsys):
        rc = pyfltr.cli.main.run(["generate-shell-completion", "powershell"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Register-ArgumentCompleter" in captured.out

    def test_no_shell_argument(self):
        with pytest.raises(SystemExit):
            pyfltr.cli.main.run(["generate-shell-completion"])

    def test_invalid_shell_argument(self):
        with pytest.raises(SystemExit):
            pyfltr.cli.main.run(["generate-shell-completion", "zsh"])
