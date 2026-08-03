---
name: test-constraints
description: >
  pyfltrのテスト・実装制約の方針。
  tomlkit統一・functools.lru_cacheによる実行内キャッシュ・関数内ローカルimportの制限・
  pyright誤検知回避・monkeypatchによる設定差し替え・エージェント検出変数およびPYFLTR_OUTPUT_FORMATの環境変数隔離・
  pre-commit hookでのuv runの--frozen指定・テストとモジュールパス参照の同期などの制約を集約する。
  pyfltr配下のPythonファイル・tests配下のPythonファイル・
  .pre-commit-config.yaml を編集する際に使用する。
---

# pyfltrのテスト・実装制約

- TOML読み書きは`tomlkit`に統一する（`tomllib`は使用しない）。
  `pyproject.toml`およびグローバル設定ファイル`config.toml`の読込・編集に適用する
- 実行内（プロセス全体で1回計算したい）キャッシュは`@functools.lru_cache(maxsize=1)`で実装する。
  モジュール変数＋`global`文の代替案よりpylint抑止が不要で、`monkeypatch.setattr`でテスト差し替えできる
- 関数内ローカルimportは「循環import発生時のみ」「オプショナル依存のtry/except内」の2用途に限定する。
  起動時間の最適化を目的とした遅延importは行わない。
  動的フォーマッター登録のような構造的事情は、レジストリ初期化を呼び出し側へ集約して回避する
- 単一のサブコマンドだけが使うサードパーティ依存は、当該依存が解決できない場合でも
  他サブコマンドと`--help`が起動するよう、importをモジュール先頭のtry/exceptで捕捉する。
  適用条件は「pyfltr配下のあるモジュールがトップレベルでimportするサードパーティパッケージのうち、
  当該パッケージを必要とする処理が単一サブコマンドの実行本体に限られる」こととする。
  捕捉した状態はモジュール変数へ保持し、当該サブコマンドの実行本体で判定して
  利用者向けのエラーメッセージと非ゼロ終了へ変換する。
  必須依存として宣言済みであっても適用する。
  配布物の版指定は下流プロジェクトの依存解決の上書きで無効化されうるため、宣言だけでは防御にならない。
  捕捉はモジュール先頭で行い、関数内ローカルimportへは移さない。
  ただし、当該パッケージの実体がモジュールの定義自体に必要な場合（基底クラス・デコレーターなど、
  import失敗時にクラス定義が成立しない用途）は捕捉が成立しないため対象外とする。
  `pyfltr/cli/mcp_models.py`の`pydantic`が該当する。
  対象外とする依存は、上書きで無効化されにくい緩い版指定で直接宣言し、版指定側で防御する
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
- pyfltrテストでは`AGENT_INDICATOR_ENVS`のいずれかまたは`PYFLTR_OUTPUT_FORMAT`が
  予期せず設定されているとjsonl既定へ切り替わる。
  `tests/conftest.py`のautouseフィクスチャ`_isolate_output_format_envs`で一律に未設定化する。
  値を設定するテストでのみ`monkeypatch.setenv`で個別に上書きする
- テストで`pyfltr.config.config.load_config()`経由の設定値を差し替えたい場合は、
  `monkeypatch.setattr(pyfltr.config.config, "load_config", lambda **_kw: <test_config>)`の形で関数自体を置換する。
  autouseフィクスチャ`_isolate_global_config`は`PYFLTR_GLOBAL_CONFIG`をtmpパスへ固定するのみで
  cwdの`pyproject.toml`は依然として読み込まれるため、`load_config`自体の差し替えが必要
- `os.path.expanduser`の`~`展開先をテストで固定する場合は、
  `monkeypatch.setenv("HOME", ...)`に加えて`monkeypatch.setenv("USERPROFILE", ...)`も同じ値で上書きする。
  Windowsの`ntpath.expanduser`は`USERPROFILE`を優先するため`HOME`単独では機能しない
- テストでツール解決パス（`shutil.which`戻り値・`commandline[0]`等）と特定ツール名を文字列比較するときは、
  `pathlib.Path(<path>).stem == "<tool>"`の形で比較する。
  Windows runnerでは`.EXE`等の拡張子が付いて返るためである
- `scripts/`配下のスクリプトを`tests/`配下から参照する場合は`scripts/__init__.py`を設置する。
  mypyは`__init__.py`の有無でファイルパスからモジュール名を決めるため、
  設置しないと同一ファイルがトップレベル名とパッケージ配下名の双方へ割り当たり検査が停止する。
  `[tool.pytest.ini_options]`の`pythonpath`は型検査器が参照しないため代替にならない
- テストコードからの実装参照には2系統があり、リファクタリング時は両方を漏れなく追従させる。
  `import`文・`from ... import ...`は静的解析で検出できるが、
  `monkeypatch.setattr("pyfltr.command.xxx....")` / `mocker.patch("pyfltr.command.xxx....")` /
  `caplog`等のlogger名指定の文字列引数は静的解析で検出できない。
  サブパッケージ移動・リネームのたびに`grep -rn 'pyfltr\.<旧パス>'`で全文検索して網羅置換する
- monkeypatchの個別事例は`tests/command_core_test.py`等の該当テストコード内コメントに集約する。
  対象は`lru_cache`付き判定関数の差し替え方法・`shutil.which`mockのモジュールパス指定・
  `run_subprocess_with_timeout`戻り値型の構築・副作用検証2段呼び出しヘルパー再利用などである
- モノレポ統合テストで対象ツールがCI環境で利用できない可能性がある場合、
  `run_subprocess`単独モックでは実行対象からドロップされ`AssertionError`となる
  - 既定の`bin-runner = "mise"`経路では`ensure_mise_available`が`mise exec -- <bin> --version`を
    実環境で実行して可用性判定する。
    `run_subprocess`のモックだけでは同判定を制御できない
  - 対象コマンドの`{command}-runner`をテスト設定で`direct`に固定したうえで
    `shutil.which`をモックし、mise経路が実環境に依存して判定する動作を回避する
  - 集約フィルターなど言語非依存の動作は、既存テストで検証済みの言語（現状は`cargo`系統合テスト）1件で
    代表検証し、他言語は`discover_subprojects`単体テストで検証する
- pyfltrテストで`pyfltr.cli.main.run([subcmd, <target>])`のスモークテストを`--commands`未指定で書く場合、
  `<target>`と`--work-dir`にリポジトリルートを渡さず`tests/conftest.py`の`_isolated_target`フィクスチャの`tmp_path`を使う。
  外側`uvx pyfltr ci`のdogfoodingと同一のリポジトリツリーへ実I/Oでアクセスする構造条件をテストが持たない形に保つため
- pytest全体の`--timeout`（`pyproject.toml`の`addopts`で指定）より長い実行を要するテストは、
  `@pytest.mark.timeout`で個別に上限を指定する。
  外側のpytestタイムアウトはテスト内部の`subprocess.run(timeout=...)`より長く保つ。
  外側が先に発火すると内部タイムアウトの明確なエラーが得られず、原因不明の失敗として現れる
