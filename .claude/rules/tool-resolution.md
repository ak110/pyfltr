# pyfltrのツール解決方針

pyfltrが対応するformatter/linter/testerの依存指定および実行時のツール解決順位を集約する。

## 対応ツールの依存方針

依存指定は以下の基準で振り分ける。

- 本体依存（`dependencies`）: 本家公式かつ自己完結なPyPIパッケージ
  - 汎用的に有用なもの（例: `typos`、`pre-commit`）
  - Python系ツール一式（例: `ruff`、`mypy`、`pylint`、`pyright`、`ty`、`pytest`、`uv-sort`）。
    `uvx pyfltr`単発で揃うようにし、`{command}-runner = "python-runner"`既定でグローバル
    `python-runner = "uv"`へ委譲し、cwdの`uv.lock`検出時は利用者プロジェクトの登録版へ切り替える。
    なお`pyright[nodejs]` extrasはインストール時にNode.jsランタイムを取得するため厳密には
   「自己完結」ではないが、Python系ツール一式を`uvx pyfltr`単発で揃える利便性を優先して同梱する
- 依存指定なし: 本家から独立した個人または別組織のメンテに依存するもの、
  インストール時に外部バイナリを取得するもの、Node.js等のランタイムを伴うもの。
  Node.js系・Goバイナリ・Rust/.NET系など利用者が個別に導入する

サードパーティの非公式PyPIラッパー（例: `shfmt-py`・`actionlint-py`・`shellcheck-py`）は、
本家から独立した個人または別組織のメンテに依存するため本体依存には組み込まない。
本家公式であってもインストール時に外部バイナリを取得するパッケージ（例: `editorconfig-checker`）は、
オフライン・プロキシ環境での導入失敗リスクを避けるため本体依存から除外する。
Node.js等のランタイムを伴うパッケージも、ランタイム導入とサプライチェーンの広さの観点から本体依存に含めない。

ネイティブバイナリツール（cargo系・dotnet系を含む）は`pyfltr/command/runner.py`の`_BIN_TOOL_SPEC`にmise backend付きで登録する。
あわせて`pyfltr/config/config.py`の`{command}-runner`既定値を`"bin-runner"`に揃える。
グローバル`bin-runner`既定値`"mise"`へ委譲することで、追加ツール導入時もmise経由の自動セットアップが既定動作となる。
利用者は`{command}-runner = "direct"`または`{command}-path`の明示で個別に切り戻せる。
新ツール追加時は本方針に従い、`_BIN_TOOL_SPEC`への登録と`{command}-runner` / `{command}-version`既定値の追加をセットで行う。

`[python]` extrasは空配列エイリアスとして残す。
本体依存にPython系ツール一式が同梱されているため、`uv add --dev "pyfltr[python]"`でも`uv add --dev pyfltr`でも利用者プロジェクトのvenvに同じものが入る。
extrasの空エイリアスは過去版からの利用者環境の`pyfltr[python]`指定をエラー化させない互換維持と、将来Python系ツールの依存配置を見直す際の表記予約のために維持する。
推奨表記はドキュメント側で`uv add --dev "pyfltr[python]"`を採用し、利用者から見える表記を将来の方針変更に対しても安定させる。

## 呼び出し方の推奨

利用者向けの推奨呼び出し方は`uvx pyfltr`（最新解決）とする。
`uv add --dev "pyfltr[python]"`でdev依存に固定し`uv run pyfltr`で呼び出す運用も選べるが、これはプロジェクト判断とする。
両者の使い分けと推奨理由のSSOTは`docs/guide/recommended.md`の「呼び出し方の使い分け」節とする。

## ツール解決の優先順位

ツールsubprocess起動時は、`{command}-runner`設定値に応じて以下の優先順位で解決する。

- `{command}-path` 明示指定: 最優先で当該パスを採用する（Python系の既定値は空文字列で「未指定」扱い）
- `{command}-runner = "python-runner"`（Python系ツールの既定）:
  グローバル`python-runner`設定（既定`"uv"`）へ委譲する。
  解決先は`"uv"` / `"uvx"` / `"direct"`のいずれかとなる
- `{command}-runner = "uv"`:
  cwdに`uv.lock`があり、かつ`uv`バイナリが利用可能な場合は`uv run --frozen <bin>`経由でプロジェクトのvenvにあるツールを呼ぶ。
  いずれかが満たされない場合は`shutil.which`で本体依存に同梱されたバイナリを直接呼ぶ（directフォールバック）
- `{command}-runner = "uvx"`:
  `uvx <bin>`形式でPyPI最新版を都度取得して起動する。`uv.lock`は参照せず、`{command}-version`設定とも連動しない。
  `uvx`shim不在時はdirectフォールバック
- `{command}-runner = "mise"`: mise経由で解決
- `{command}-runner = "direct"`: PATH解決
- `{command}-runner = "js-runner"`: グローバル`js-runner`設定（pnpx/pnpm/npm/npx/yarn/direct）へ委譲
- `{command}-runner = "pnpx"` / `"pnpm"` / `"npm"` / `"npx"` / `"yarn"`: per-tool直接指定でJSパッケージマネージャー経由で解決

cwdのuvプロジェクトに対象ツールが登録されていない場合、`uv run`側がエラーで失敗する。
利用者は当該ツールをプロジェクトに追加するか、`{command}-path`で明示するか、
`{command}-runner`を`"direct"`へ切り替えて対応する。

ツール解決経路の追跡情報はJSONL header（`uv_lock_present`・`uv_available`）と各commandレコード（`effective_runner`・`runner_source`）に出力する。
利用者・LLMが「想定どおりuv経路で動作したか」「direct fallbackが起きていないか」を出力から判別できるようにするための情報である。
JSONLフィールドの追加・名称変更は[output方針](output.md)に従う。

## {command}-runnerの値の体系

`{command}-runner`はper-tool設定で、以下の2種類の値を取る。両者は対等な選択肢として並ぶ。

- カテゴリ委譲値: `python-runner` / `js-runner` / `bin-runner`。
  各カテゴリのグローバル設定値へ委譲する。
  Python系tool・JS系tool・ネイティブ系toolの既定値はそれぞれ対応するカテゴリ委譲値とする
- 直接指定値: `direct` / `mise` / `uv` / `uvx` / `pnpx` / `pnpm` / `npm` / `npx` / `yarn`。
  per-toolで実装ツールを直接指定する場合に使う

カテゴリ委譲値のグローバル設定は以下。

- `python-runner`許容値: `("direct", "uv", "uvx")`、既定値`"uv"`
- `js-runner`許容値: `("direct", "pnpx", "pnpm", "npm", "npx", "yarn")`、既定値`"pnpx"`
- `bin-runner`許容値: `("direct", "mise")`、既定値`"mise"`

直接指定値のカテゴリ横断バリデーション（例: `mypy-runner = "pnpm"`）は弾かない。
無意味な組み合わせは実行時に解決ロジックがエラー終了する。
実装簡潔さを優先した方針。
