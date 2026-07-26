"""`--commands`指定の展開と、未有効化コマンド警告の判定。

実行系サブコマンドが受け取る`--commands`の生値（`action="append"`のリスト）を
コマンド名列へ展開する`flatten_commands_arg`と、
そのうち有効化されておらず実行されないものを警告対象として抽出する
`compute_unmet_commands`を担う。エイリアス指定時の抑止仕様は
`compute_unmet_commands`のdocstringをSSOTとする。
"""

import pyfltr.config.config


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
