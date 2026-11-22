import pytest

import pyfltr.cli.main
import pyfltr.cli.parser
import pyfltr.cli.shell_completion
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.tools


class TestCollectCompletions:
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
        choices = next(line.split('"')[1].split() for line in script.splitlines() if "local output_formats=" in line)
        assert {"text", "jsonl"} <= set(choices)

    def test_commands_choices_contain_builtin_and_aliases(self):
        parser = pyfltr.cli.parser.build_parser()
        script = pyfltr.cli.shell_completion.generate("bash", parser, frozenset())
        for name in pyfltr.tools.BUILTIN_COMMAND_NAMES:
            assert name in script
        for alias in pyfltr.config.model.DEFAULT_CONFIG["aliases"]:
            assert alias in script


class TestGenerateBash:
    def test_contains_function_and_complete(self):
        parser = pyfltr.cli.parser.build_parser()
        subcommands = frozenset({"ci", "run", "fast", "generate-shell-completion"})
        script = pyfltr.cli.shell_completion.generate("bash", parser, subcommands)
        assert "_pyfltr_completions()" in script
        assert "complete -o default -F _pyfltr_completions pyfltr" in script


class TestGeneratePowershell:
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
    def test_bash_success(self, capsys):
        rc = pyfltr.cli.main.run(["generate-shell-completion", "bash"])
        assert rc == 0
        captured = capsys.readouterr()
        choices = next(line.split('"')[1].split() for line in captured.out.splitlines() if "local subcommands=" in line)
        assert {"ci", "run", "generate-shell-completion"} <= set(choices)

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
