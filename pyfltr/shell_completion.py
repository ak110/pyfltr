"""シェル補完スクリプトの生成。"""

import argparse

import pyfltr.config

SUPPORTED_SHELLS: tuple[str, ...] = ("bash", "powershell")
"""対応シェル。"""


def generate(
    shell: str,
    parser: argparse.ArgumentParser,
    subcommands: frozenset[str],
) -> str:
    """シェル種別に応じた補完スクリプトを返す。"""
    options, output_format_choices, commands_choices = _collect_completions(parser)
    subcommand_list = sorted(subcommands)
    if shell == "bash":
        return _generate_bash(options, output_format_choices, commands_choices, subcommand_list)
    if shell == "powershell":
        return _generate_powershell(options, output_format_choices, commands_choices, subcommand_list)
    raise ValueError(f"未対応のシェル: {shell!r}")


def _collect_completions(
    parser: argparse.ArgumentParser,
) -> tuple[list[str], list[str], list[str]]:
    """パーサーからオプション名・choices・コマンド名を収集する。

    戻り値: (options, output_format_choices, commands_choices)
    """
    options: list[str] = []
    output_format_choices: list[str] = []
    for action in parser._actions:  # noqa: SLF001  # pylint: disable=protected-access
        for opt in action.option_strings:
            options.append(opt)
        if "--output-format" in action.option_strings and action.choices:
            output_format_choices = list(action.choices)

    # --commands の補完候補: ビルトインコマンド名 + 静的エイリアスキー
    commands_choices = list(pyfltr.config.BUILTIN_COMMAND_NAMES)
    aliases = pyfltr.config.DEFAULT_CONFIG.get("aliases", {})
    assert isinstance(aliases, dict)
    for alias_name in aliases:
        if alias_name not in commands_choices:
            commands_choices.append(alias_name)
    commands_choices.sort()

    return sorted(options), sorted(output_format_choices), commands_choices


def _generate_bash(
    options: list[str],
    output_format_choices: list[str],
    commands_choices: list[str],
    subcommands: list[str],
) -> str:
    """bash用補完スクリプトを生成する。"""
    opts = " ".join(options)
    subs = " ".join(subcommands)
    shells = " ".join(SUPPORTED_SHELLS)
    fmt_choices = " ".join(output_format_choices)
    cmd_choices = " ".join(commands_choices)

    return f'''\
_pyfltr_completions() {{
    local cur prev words cword
    _init_completion || return

    local subcommands="{subs}"
    local options="{opts}"
    local shells="{shells}"
    local output_formats="{fmt_choices}"
    local commands="{cmd_choices}"

    # generate-shell-completionの第2引数: シェル名を補完
    if [[ ${{cword}} -ge 2 && "${{words[1]}}" == "generate-shell-completion" ]]; then
        COMPREPLY=( $(compgen -W "${{shells}}" -- "${{cur}}") )
        return
    fi

    # --output-format / --output-format= の補完
    if [[ "${{prev}}" == "--output-format" ]]; then
        COMPREPLY=( $(compgen -W "${{output_formats}}" -- "${{cur}}") )
        return
    fi
    if [[ "${{cur}}" == --output-format=* ]]; then
        local prefix="${{cur%%=*}}="
        local typed="${{cur#*=}}"
        COMPREPLY=( $(compgen -W "${{output_formats}}" -- "${{typed}}") )
        COMPREPLY=( "${{COMPREPLY[@]/#/${{prefix}}}}" )
        return
    fi

    # --commands / --commands= の補完
    if [[ "${{prev}}" == "--commands" ]]; then
        COMPREPLY=( $(compgen -W "${{commands}}" -- "${{cur}}") )
        return
    fi
    if [[ "${{cur}}" == --commands=* ]]; then
        local prefix="${{cur%%=*}}="
        local typed="${{cur#*=}}"
        COMPREPLY=( $(compgen -W "${{commands}}" -- "${{typed}}") )
        COMPREPLY=( "${{COMPREPLY[@]/#/${{prefix}}}}" )
        return
    fi

    # 第1引数: サブコマンド + オプション
    if [[ ${{cword}} -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "${{subcommands}} ${{options}}" -- "${{cur}}") )
        return
    fi

    # オプション開始
    if [[ "${{cur}}" == -* ]]; then
        COMPREPLY=( $(compgen -W "${{options}}" -- "${{cur}}") )
        return
    fi

    # ファイル/ディレクトリ補完
    _filedir
}}

complete -o default -F _pyfltr_completions pyfltr
'''


def _generate_powershell(
    options: list[str],
    output_format_choices: list[str],
    commands_choices: list[str],
    subcommands: list[str],
) -> str:
    """PowerShell用補完スクリプトを生成する。"""
    # PowerShellの配列リテラルとして出力
    opts_ps = ", ".join(f"'{o}'" for o in options)
    subs_ps = ", ".join(f"'{s}'" for s in subcommands)
    shells_ps = ", ".join(f"'{s}'" for s in SUPPORTED_SHELLS)
    fmt_ps = ", ".join(f"'{f}'" for f in output_format_choices)
    cmd_ps = ", ".join(f"'{c}'" for c in commands_choices)

    return f"""\
Register-ArgumentCompleter -Native -CommandName pyfltr -ScriptBlock {{
    param($wordToComplete, $commandAst, $cursorPosition)

    $subcommands = @({subs_ps})
    $options = @({opts_ps})
    $shells = @({shells_ps})
    $outputFormats = @({fmt_ps})
    $commands = @({cmd_ps})

    $tokens = $commandAst.ToString().Substring(0, $cursorPosition).Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries)
    $tokenCount = $tokens.Count

    # generate-shell-completionの第2引数: シェル名を補完
    if ($tokenCount -ge 2 -and $tokens[1] -eq 'generate-shell-completion') {{
        $shells | Where-Object {{ $_ -like "$wordToComplete*" }} | ForEach-Object {{
            [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
        }}
        return
    }}

    # --output-format の値補完
    if ($tokenCount -ge 2 -and $tokens[-1] -eq '--output-format') {{
        $outputFormats | Where-Object {{ $_ -like "$wordToComplete*" }} | ForEach-Object {{
            [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
        }}
        return
    }}
    if ($wordToComplete -like '--output-format=*') {{
        $typed = $wordToComplete.Substring('--output-format='.Length)
        $outputFormats | Where-Object {{ $_ -like "$typed*" }} | ForEach-Object {{
            $val = "--output-format=$_"
            [System.Management.Automation.CompletionResult]::new($val, $val, 'ParameterValue', $_)
        }}
        return
    }}

    # --commands の値補完
    if ($tokenCount -ge 2 -and $tokens[-1] -eq '--commands') {{
        $commands | Where-Object {{ $_ -like "$wordToComplete*" }} | ForEach-Object {{
            [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
        }}
        return
    }}
    if ($wordToComplete -like '--commands=*') {{
        $typed = $wordToComplete.Substring('--commands='.Length)
        $commands | Where-Object {{ $_ -like "$typed*" }} | ForEach-Object {{
            $val = "--commands=$_"
            [System.Management.Automation.CompletionResult]::new($val, $val, 'ParameterValue', $_)
        }}
        return
    }}

    # 第1引数: サブコマンド + オプション
    if ($tokenCount -le 1 -or ($tokenCount -eq 2 -and $wordToComplete -ne '')) {{
        $all = $subcommands + $options
        $all | Where-Object {{ $_ -like "$wordToComplete*" }} | ForEach-Object {{
            $type = if ($_ -like '-*') {{ 'ParameterName' }} else {{ 'ParameterValue' }}
            [System.Management.Automation.CompletionResult]::new($_, $_, $type, $_)
        }}
        return
    }}

    # オプション開始
    if ($wordToComplete -like '-*') {{
        $options | Where-Object {{ $_ -like "$wordToComplete*" }} | ForEach-Object {{
            [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterName', $_)
        }}
        return
    }}
}}
"""
