---
paths:
  - "pyfltr/**/*.py"
  - "tests/**/*.py"
  - ".pre-commit-config.yaml"
---

# pyfltrのテスト・実装制約

- TOML読み書きは`tomlkit`に統一する（`tomllib`は使用しない）。
  `pyproject.toml`およびグローバル設定ファイル`config.toml`の読込・編集に適用する
- 実行内（プロセス全体で1回計算したい）キャッシュは`@functools.lru_cache(maxsize=1)`で実装する。
  モジュール変数＋`global`文の代替案よりpylint抑止が不要で、
  `monkeypatch.setattr`でテスト差し替えできる
- 関数内ローカルimportは「循環import発生時のみ」「オプショナル依存のtry/except内」の2用途に限定する。
  起動時間の最適化を目的とした遅延importは行わない。
  動的フォーマッター登録のような構造的事情は、レジストリ初期化を呼び出し側へ集約して回避する
- 同一サブパッケージ内のモジュール間importは、`pyright`が関数内ローカルimportを未解決として誤検知する事象がある。
  特に`pyfltr/command/dispatcher.py`はモジュールレベルimportで統一し、
  循環import発生時のみローカルimportに切り替える
- インライン抑止コメント（`# pylint: disable=`・`# noqa`・`# type: ignore`等）は、
  ルール本来の意図が当該箇所に当てはまらない例外を局所的に示す目的に限定する。
  構造的問題の回避手段として使わない。
  やむを得ず残す場合は同一行または直前行に理由コメントを併記する。
  同一抑止が複数箇所で必要になる場合は設定ファイル側での扱いをユーザーと相談する
- pre-commit hookの`entry:`で`uv run`を起動する場合、必ず`--frozen`を明示する。
  pre-commitは親の環境変数を引き継がない構成のため`UV_FROZEN`が未設定で到達する可能性がある
- pyfltrテストでは`AI_AGENT` / `PYFLTR_OUTPUT_FORMAT`が予期せず設定されているとjsonl既定へ切り替わる。
  `tests/conftest.py`のautouseフィクスチャ`_isolate_output_format_envs`で両環境変数を未設定にする。
  値を設定するテストでのみ`monkeypatch.setenv`で個別に上書きする
- テストで`pyfltr.config.config.load_config()`経由の設定値を差し替えたい場合は、
  `monkeypatch.setattr(pyfltr.config.config, "load_config", lambda **_kw: <test_config>)`の形で
  関数自体を置換する。
  autouseフィクスチャ`_isolate_global_config`は`PYFLTR_GLOBAL_CONFIG`をtmpパスへ固定するのみで
  cwdの`pyproject.toml`は依然として読み込まれるため、`load_config`自体の差し替えが必要
- テストコードからの実装参照には2系統があり、リファクタリング時は両方を漏れなく追従させる。
  `import`文・`from ... import ...`は静的解析で検出できるが、
  `monkeypatch.setattr("pyfltr.command.xxx....")` / `mocker.patch("pyfltr.command.xxx....")` /
  `caplog`等のlogger名指定の文字列引数は静的解析で検出できない。
  サブパッケージ移動・リネームのたびに`grep -rn 'pyfltr\.<旧パス>'`で全文検索して網羅置換する
- テストコードから`pyfltr.command.runner`内の`shutil.which`をmockする場合、
  `monkeypatch.setattr("pyfltr.command.runner.shutil.which", ...)`のように
  モジュールパス単位でターゲットを明示する
- `pyfltr.command.process.run_subprocess_with_timeout`をmonkeypatchで差し替える場合、
  戻り値型はテスト用ダミークラスを定義しない。
  代わりに`pyfltr.command.process.CompletedProcessWithTimeoutInfo`を直接構築する。
  ダミークラス定義はpylintの`too-few-public-methods`を誘発し、
  テスト関数内での`class _FakeProc:`記述は`redefined-outer-name`も併発する
- テストでツール解決パス（`shutil.which`戻り値・`commandline[0]`等）と特定ツール名を文字列比較する場合は、
  Windows runnerでは`.EXE`等の拡張子が付いて返るため、
  `pathlib.Path(<path>).stem == "<tool>"`の形で比較する
- `os.path.expanduser`の`~`展開先をテストで固定する場合は、
  `monkeypatch.setenv("HOME", ...)`に加えて`monkeypatch.setenv("USERPROFILE", ...)`も同じ値で上書きする。
  Windowsの`ntpath.expanduser`は`USERPROFILE`を優先するため`HOME`単独では機能しない
- `pyfltr/command/runner.py`の`@functools.lru_cache(maxsize=1)`デコレーター付き判定関数群
 （`cwd_has_uv_lock` / `ensure_uv_available` / `ensure_uvx_available`等）はプロセス内固定化される。
  テストで判定値を差し替える場合は関数自体を
  `monkeypatch.setattr("pyfltr.command.runner.cwd_has_uv_lock", lambda: True)`の形で置換する。
  新規にキャッシュ付き判定関数を追加した場合も同じ制約が適用される
- Python 3.11のargparseで`nargs="?"`位置引数と`nargs="*"`位置引数を併用するとき、次の挙動に注意する。
  両者の間に`--option=value`形式の引数が介在すると、後続位置引数がunrecognizedとして拒否される。
  grep/replaceサブコマンドのように複数位置引数（`pattern + paths`）をオプション混在で受理するときは、
  `parse_known_args`で残余を回収し位置引数列へ統合する経路で回避する。
  実装は`pyfltr/cli/main.py`の`grep`/`replace`分岐を参照する
