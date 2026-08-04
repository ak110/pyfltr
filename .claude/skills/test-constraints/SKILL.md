---
name: test-constraints
description: >
  pyfltrのテスト・実装制約の方針。
  tomlkit統一・functools.lru_cacheによる実行内キャッシュ・関数内ローカルimportの制限・
  pyright誤検知回避・monkeypatchによる設定差し替え・エージェント検出変数およびPYFLTR_OUTPUT_FORMATの環境変数隔離・
  pre-commit hookでのuv runの--frozen指定・テストとモジュールパス参照の同期・
  パス比較の区切り表現などの制約を集約する。
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
- `.pre-commit-config.yaml`の`types_or`は、`fast`サブセットに含まれ対象種別を特定できるツールの
  種別を漏らさず列挙する。
  pre-commit・prekは一致するファイルがコミットに含まれないとhook自体を起動しないため、
  欠けた種別だけを変更したコミットでは当該ツールが実行されない。
  `[tool.pyfltr]`でツールを追加で有効化した場合は`types_or`の追随要否を確認する。
  `prek`・`ec`・`typos`・`colloquial-check`のような全ファイル対象のツールは列挙では網羅できず、
  `make test`で担保する
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
- テストでパスを比較する場合、比較対象の値が区切り正規化を経ているかを実装で確認してから比較方法を選ぶ。
  正規化を経た値は`as_posix()`との文字列比較で契約を固定し、経ていない値は`str()`によるOSネイティブ表現と比較する。
  値の生成経路を確認できない場合は`pathlib.Path`同士の比較を用いず、生成箇所の実装を読んで確定する。
  `pathlib.Path`同士の比較はWindowsで区切りの差を吸収するため、契約からの退行を検出できない。
  公開する`file`値の区切り表現は`docs/development/architecture.md`が定める。
  ただし検体のパス自体がWindows区切りを含む場合（`pathlib.Path(r"nested\x.py")`のような検体）は、
  POSIX環境で`as_posix()`が当該区切りをファイル名の一部として扱い正規化しないため、
  期待値を`pyfltr.paths.normalize_separators()`で生成する。
  この場合は実装と同じ関数を用いるため、当該関数自体の退行は`tests/paths_test.py`が担保する
- 公開値の生成経路を変更する場合、当該値を検証している既存テストを実装側から逆引きで洗い出し、
  期待値の表現を追随させる。洗い出しは変更した生成箇所ごとに行い、
  同一テストファイル内の類似記述を揃える形で代替しない。
  既存テストは作成時点では正しい期待値を持つため、実装側の契約変更で初めて誤りになる。
  区切り文字の表現差による不一致は、実際のパスを用いるテストではLinux上で両表現が一致するため、
  `make test`で検出できずWindows環境のCIで顕在化する。
  `pathlib.PureWindowsPath`で表現を模擬すればLinux上でも再現できる
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
- bin-runner経路の可用性判定（`ensure_mise_available`が起動する`mise exec <tool spec> -- <bin> --version`）は、
  `tests/conftest.py`のautouseフィクスチャ`_default_mise_exec_check_success`が既定で成功へ固定する。
  同判定は`pyfltr.command.process.run_subprocess`を通らないため`run_subprocess`のモックでは抑止できず、
  既定モックが無い状態では実行環境のツール導入状態とネットワークへ依存して所要時間が変動する
  - mise解決経路の実挙動を検証するテストは`@pytest.mark.real_mise_subprocess`を付けて既定モックを外す。
    同フィクスチャはマーカー判定で早期returnするため、外した側では実装本体がそのまま呼ばれる
  - 集約フィルターなど言語非依存の動作は、既存テストで検証済みの言語（現状は`cargo`系統合テスト）1件で
    代表検証し、他言語は`discover_subprojects`単体テストで検証する
- pyfltrテストで`pyfltr.cli.main.run([subcmd, <target>])`のスモークテストを`--commands`未指定で書く場合、
  `<target>`と`--work-dir`にリポジトリルートを渡さず`tests/conftest.py`の`_isolated_target`フィクスチャの`tmp_path`を使う。
  外側`uvx pyfltr ci`のdogfoodingと同一のリポジトリツリーへ実I/Oでアクセスする構造条件をテストが持たない形に保つため
- pytest全体の`--timeout`（`pyproject.toml`の`addopts`で指定）より長い実行を要するテストは、
  `@pytest.mark.timeout`で個別に上限を指定する。
  外側のpytestタイムアウトはテスト内部の`subprocess.run(timeout=...)`より長く保つ。
  外側が先に発火すると内部タイムアウトの明確なエラーが得られず、原因不明の失敗として現れる
  - pytest組み込みの`faulthandler_timeout`は全テスト共通の秒数を使い、個別の`timeout`マーカーとは独立して動作する。
    `faulthandler_timeout`より長い正常実行を許容するテストでは、
    `@pytest.mark.usefixtures("_disable_faulthandler_timeout")`を付けて通常実行中のダンプ予約を解除する
