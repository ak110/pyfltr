"""実行系サブコマンドの既定値解決と`--commands`指定の検証。

実行系サブコマンドの既定値を解決する`apply_subcommand_defaults`、
`--commands`の生値をコマンド名列へ展開する`flatten_commands_arg`、
登録済みコマンド名を検証する`validate_commands`、有効化されておらず
実行されないコマンドを抽出する`compute_unmet_commands`を担う。
"""

import argparse
import difflib

import pyfltr.cli.output_format
import pyfltr.config.config


def apply_subcommand_defaults(args: argparse.Namespace) -> None:
    """サブコマンドごとの既定値を`args`に反映する。

    `subparsers.add_parser(..., parents=[common])`で共通オプションを継承する
    構造上、`sub_parser.set_defaults(...)`は他サブパーサーのdefaultまで
    上書きしてしまうため（argparseの既知挙動）、argparse本体の既定値機構は
    使わずここで手動解決する。CLI明示値（`store_true`や値指定）は
    事前にargsに載っているため、既定値注入は「未指定扱いの値」を上書きする
    形にとどめる。

    サブコマンド挙動:
        - `ci`: fixステージ無効。exit_zero_even_if_formattedは明示時のみTrue
        - `run`: fixステージ有効。exit_zero_even_if_formattedをTrueに
        - `fast`: runと同じ + `--commands`未指定なら`"fast"`
        - `run-for-agent`: runと同じ。`--output-format`の既定値は`_resolve_output_format`側で
          サブコマンド既定値`"jsonl"`として注入し、`PYFLTR_OUTPUT_FORMAT`での変更を許容する。
          互換維持のためのサブコマンドで、通常は`run`を使う。

    `--quiet`の既定値は`run-for-agent`のとき、または`detect_agent_indicator()`が
    エージェント実行を示す環境変数を検出したときに`True`とする。
    エージェント検出環境下では出力形式も`jsonl`へ切り替わるため、`run`と`run-for-agent`が
    等価に振る舞う。CLIで`--quiet` / `--no-quiet`を明示した場合はそちらを優先する。
    """
    subcommand = args.subcommand
    args.include_fix_stage = subcommand in ("run", "fast", "run-for-agent")
    if subcommand in ("run", "fast", "run-for-agent"):
        args.exit_zero_even_if_formatted = True
    if subcommand == "fast" and args.commands is None:
        # `--commands`は`action="append"`化によりリストで保持する。
        args.commands = ["fast"]
    if getattr(args, "quiet", None) is None:
        args.quiet = subcommand == "run-for-agent" or pyfltr.cli.output_format.detect_agent_indicator() is not None


def flatten_commands_arg(values: list[str] | None, config: pyfltr.config.config.Config) -> list[str]:
    """`--commands` で渡されたリスト（複数回指定の集合）をコマンド名配列に展開する。

    各要素にはカンマ区切りで複数のコマンドを含められるため、splitした上で
    先頭出現を優先した重複除去を行う。`None` の場合は設定上の全登録コマンド
    （ビルトイン + custom-commands）を返す。
    """
    if values is None:
        return list(config.command_names)
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        for name in raw.split(","):
            if name == "" or name in seen:
                continue
            seen.add(name)
            result.append(name)
    return result


def validate_commands(commands: list[str], config: pyfltr.config.config.Config) -> None:
    """コマンド名が設定へ登録済みであることを検証する。

    未知のコマンド名を検出した場合は、`difflib`による候補提示を含む
    メッセージで`ValueError`を送出する。CLI経路は`parser.error`へ、
    MCP経路はMCPエラーへ変換する。
    """
    for command in commands:
        if command not in config.commands:
            suggestions = difflib.get_close_matches(command, list(config.commands), n=3, cutoff=0.6)
            suffix = f"。もしかして: {', '.join(suggestions)}" if suggestions else ""
            raise ValueError(f"コマンドが見つかりません: {command}{suffix}")


def compute_unmet_commands(
    command_tokens: list[str],
    requested_commands: list[str],
    enabled_commands: list[str],
    config: pyfltr.config.config.Config,
) -> list[str]:
    """`--commands`指定のうち未有効化で未実行となるコマンドを警告対象として抽出する。

    判定仕様は次のとおり。

    - `command_tokens`は`--commands`の生値を`flatten_commands_arg`で展開したトークン列
    - `config["aliases"]`のキーであるトークンをエイリアス指定、そうでないトークンを個別指定とみなす
    - エイリアス指定は展開結果に有効化済みコマンドが1件以上あれば展開結果を警告対象から除外する。
      1件も無い場合は展開結果を警告対象に残す
    - 個別指定は常に警告対象とする。同一コマンドがエイリアス経由と個別指定の両方で
      指定された場合は個別指定を優先し警告対象に残す

    エイリアスは「該当するものを実行する」意味で使われるため、
    一部のみ有効な構成（JavaScriptのみのプロジェクトでの`--commands=audit`など）では
    警告を発行しない。全て無効なら実行対象が空になるため警告を残す。

    Returns:
        `requested_commands`の順序を保った未有効化コマンド一覧。
    """
    enabled = set(enabled_commands)
    suppressed: set[str] = set()
    explicit: set[str] = set()
    for token in command_tokens:
        if token in config["aliases"]:
            expanded = pyfltr.config.config.resolve_aliases([token], config)
            if any(name in enabled for name in expanded):
                suppressed.update(expanded)
        else:
            explicit.add(token)
    return [c for c in requested_commands if c not in enabled and (c in explicit or c not in suppressed)]
