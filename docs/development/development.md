# 開発手順

## 開発環境の構築手順

1. 本リポジトリをcloneする
2. 初回セットアップを実行する

    ```bash
    make setup
    ```

## 開発コマンド

| コマンド | 用途 |
| --- | --- |
| `make format` | 整形 + 軽量lint（開発時の手動実行用） |
| `make test` | 全チェック実行（コミット前に通過させる。コーディングエージェントからの検証もこれを使う） |
| `make update` | 依存更新 + prek autoupdate + アクション更新 + 全テスト |
| `uv run mkdocs serve` | ドキュメントのローカルプレビュー |

## サプライチェーン攻撃対策

ロック尊重・公開待機・ピン留め運用の3点を採用している。

- `uv.lock` を尊重するため `UV_FROZEN=1` を常時有効化している（Makefile・CI・pre-commitフック経由）。
  適用経路の詳細はMakefile側コメントに委ねる
- `pyproject.toml` の `exclude-newer` で公開直後パッケージを一定期間除外し、サプライチェーン汚染リスクを低減する
    - `.npmrc` の `minimum-release-age` で、pnpm経由で解決するパッケージにも同じ公開待機（1440分）を適用する。
      JS系ツールをmise・pnpm経由で起動するため、Python系の `exclude-newer` と対で必要となる
- GitHub Actionsのサードパーティアクションはハッシュピン留めで固定する（`make update-actions`で更新）

依存パッケージの脆弱性検知はDependabot alertsと定期監査ワークフロー（`audit.yaml`）の2経路で行う。
本リポジトリはPyPI配布のコマンドラインツールであり、利用者は`uvx`等で依存を解決したうえで実行するため、
依存の脆弱性が利用者の実行環境へ波及する。ライブラリとは利用形態が異なるため検知の仕組みを設ける。
Dependabotによる自動修正PRの作成は無効とし、更新は`make update`の依存更新へ集約する。
`[tool.uv] override-dependencies`による上書き設定は本リポジトリの依存解決にのみ適用され、
配布物のメタデータには含まれない。
そのため`uvx`等で配布物を取得して依存を解決する利用者環境と、
同じ方式でpyfltrを導入する公式Dockerイメージには波及しない。
上流パッケージの厳密ピンにより依存更新だけでは解消できない場合の上書きによる迂回の採否は、
`agent-toolkit:coding-standards`の「依存管理」の項を参照する。
当該の項は`~/dotfiles/agent-toolkit/skills/coding-standards/SKILL.md`の
「コーディング品質（全言語共通）」節にある。
`[project] dependencies`と`[project.optional-dependencies]`が宣言する依存、および
それらから推移的に解決される依存は利用者環境へ波及する。
開発用の依存グループ経由でのみ入る依存は波及しない。
迂回する場合も迂回しない場合も、その判断の根拠と解除条件を
`pyproject.toml`の当該指定へ付すコメントへ残す。

`pyproject.toml`の`dependencies`または`override-dependencies`でパッケージの版指定を変更した場合、
`uv lock`・`uv sync`・`uv run`の成功だけでは配布経路の成立を確認できない。
上書き設定が適用されない状態で依存解決が成立することを
`uvx --exclude-newer "1 day" --from . pyfltr --version`で実測する。
当該コマンドは利用者環境と同じ経路で配布物の依存を解決するため、
上書き設定に依存した版指定を検出できる。
`uvx`は`pyproject.toml`の`[tool.uv]`を読まず`exclude-newer`が適用されない。
公開待機を維持するため`--exclude-newer`を明示する。
当該コマンドが解決するのは配布物のメタデータが宣言する実行時依存に限る。
開発用の依存グループだけに適用される上書きは観測できない。
`make test`は本リポジトリの依存解決のみを用いるため当該不整合を検出しない。
実測が失敗した場合は原因を確認する。
通信障害・パッケージ索引の障害・ビルド環境の不備など、版指定以外の原因を解消して再実測する。
変更した版指定に起因する依存解決不能を確認した場合は、配布物のインストールを不能にするため
当該版指定を採用しない。

`override-dependencies`の追加・変更は開発環境の依存解決にも影響する。
上書きにより開発用の依存グループが要求する版が満たされなくなると、当該の依存へ依存する
開発用ツールが起動しなくなる。前段のどの手順もこの不成立を観測しない。
上書きを追加または変更した場合は、`uv lock`で変更をロックファイルへ反映したうえで、
上書き対象のパッケージへ依存する開発用ツールを`uv run --locked <ツール名> --help`のような
軽量な指定で起動し、終了コードが0であることを実測する。
`--locked`は`uv.lock`が変更されないことを表明する指定であり、
ロックファイルへ未反映の変更が残っている場合に実測を失敗させる。
`--frozen`は`uv.lock`を更新せずに実行する指定であり、上書きを変更した直後は
旧ロックから同期した環境を観測するため検出漏れを起こす。
起動しない状態を許容する場合は、影響範囲・許容する理由・解除条件を
`pyproject.toml`の当該`override-dependencies`のコメントへ残す。

依存の版指定を変更する場合、および特定パッケージの版を引き上げられるかを判断する場合は、
`uv tree --frozen --invert --package <名前>`で当該パッケージの逆依存を全件列挙し、
各逆依存がPyPIの`requires_dist`で課している制約を個別に確認する。
依存解決は複数の制約の連立であり、単一パッケージのメタデータだけでは充足を判定できない。
上限制約を課す逆依存が1件でもある場合、他の逆依存が下限を引き上げていても解決は成立しない。

`uv lock`は制約が緩んでも既存のロック内容を保持し、自動では版を上げない。
特定パッケージの版を引き上げる場合は`--upgrade-package <名前>`を、
全体を更新する場合は`--upgrade`を指定する。
制約を変更したのに版が変わらない場合は、まず再解決の範囲を疑う。
他の依存が課す制約を原因と断定する前に、上記の指定を伴う再実行で確認する。

定期監査ワークフロー（`audit.yaml`）はLinux単独構成とし、プラットフォーム軸のmatrixを設けない。
`uv audit`は`--python-platform`を受理するが、ロックファイルを持つプロジェクトでは
監査対象を当該プラットフォームの依存へ限定しない。
2026-08-02にuv 0.11.7で実測したところ、`--python-platform linux`と`--python-platform windows`は
いずれも`in 150 packages`を返した。
一方`uv tree`は限定する。同じ実測で`--python-platform windows`にのみ`pywin32`・`colorama`が現れ、
`--python-platform linux`にのみ`h11`が現れる。
すなわちロックファイルにWindows固有依存は実在するが、`uv audit`はそれを除外も追加もしない。
そのためmatrixを追加しても監査対象は広がらず、CI実行時間だけが増える。
`uv audit`は実験的機能であり、公式のCLIリファレンスは`--python-platform`を
対象プラットフォームの依存を監査するオプションとして説明する。
実装の挙動は当該説明と一致しないため、uv側の更新で挙動が変わる可能性がある。
本節の根拠を更新する際は上記の実測を再実行し、実測日と使用したuvのバージョンを併記する。

## 設定ファイルの判断根拠

値のみのファイルやJSON形式のファイルは、判断根拠を当該ファイルへ残せないため本節へ記録する。

`.editorconfig-checker.json`はMarkdown（`\.md$`）を除外し`IndentSize`を無効化する。
Markdownの除外は、editorconfig-checkerがコードブロック内のタブ等を`.editorconfig`違反として
誤検知するためである。
`IndentSize`の無効化はMarkdown以外のファイルのために必要である。
無効化を外して`--commands=ec`を実行すると184件を検出し、
その内訳はPythonのdocstring本文の継続行と、YAMLのブロックスカラー内のシェル継続行である。
いずれも`.editorconfig`が指定する`indent_size`の倍数から外れるが、
構造を読み取れるように意図した字下げであり、整形の対象でもない。

`.python-version`の値は`pyproject.toml`の`requires-python`の下限に一致させる。
ローカル開発環境を最小サポート版とし、新しい版でのみ通る記述の混入を開発時点で検出するためである。
`requires-python`の下限を引き上げる場合は`.python-version`とCIマトリクスの下限も同時に更新する。

## ドキュメントサイト運用

ドキュメントはMkDocsで管理し、GitHub Pagesでホスティングする。

### mkdocs.yml編集時の注意

`mkdocs.yml`の`nav`を変更した場合は`uv run mkdocs build --strict`で
リンク切れや設定ミスがないことを確認する。
`llmstxt`プラグインの`sections`設定もnavに合わせて更新する。

masterブランチへのpush時にdocs/配下やmkdocs.ymlの変更があると自動デプロイする。

## リリース手順

事前に`gh`コマンドをインストールして`gh auth login`でログインし、以下のいずれかを実行する。

```bash
gh workflow run release.yaml --field="bump=PATCH"
gh workflow run release.yaml --field="bump=MINOR"
gh workflow run release.yaml --field="bump=MAJOR"
```

<https://github.com/ak110/pyfltr/actions> で状況を確認する。

リリースを伴わず`ghcr.io/ak110/pyfltr:latest`のみを再発行する場合は、
`docker-build.yaml`を単発で起動する。
base OS更新や同梱ツールのglibc要件変動を`latest`へ反映するときの運用パス。

```bash
gh workflow run docker-build.yaml
gh workflow run docker-build.yaml --field="version=X.Y.Z"
```

`--field="version="`未指定時はPyPIから最新公開版を取得し、`vX.Y.Z`と`latest`の両タグを併発行する。
