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
- GitHub Actionsのサードパーティアクションはハッシュピン留めで固定する（`make update-actions`で更新）

依存パッケージの脆弱性検知はDependabot alertsと定期監査ワークフロー（`audit.yaml`）の2経路で行う。
本リポジトリはPyPI配布のコマンドラインツールであり、利用者は`uvx`等で依存を解決したうえで実行するため、
依存の脆弱性が利用者の実行環境へ波及する。ライブラリとは利用形態が異なるため検知の仕組みを設ける。
Dependabotによる自動修正PRの作成は無効とし、更新は`make update`の依存更新へ集約する。
上流パッケージの厳密ピンにより依存更新だけでは解消できない場合の対処は、
当該脆弱性が配布物のメタデータを経由して利用者環境へ波及するかで分ける。
`[tool.uv] override-dependencies`による上書き設定は本リポジトリの依存解決にのみ適用され、
配布物のメタデータには含まれない。
そのため`uvx`等で配布物を取得して依存を解決する利用者環境と、
同じ方式でpyfltrを導入する公式Dockerイメージには波及しない。
利用者環境へ波及する脆弱性を上書き設定で迂回すると、
本リポジトリの監査結果だけが緑になり、利用者環境の脆弱性は未解消のまま残る。
脆弱性検知の目的に対する抑止として働くため、当該の迂回は行わない。
監査結果を維持したまま上流パッケージ側のピン追従を待ち、
迂回しない判断とその根拠・解除条件を該当箇所のコメントへ残す。
開発時にのみ用いる依存で利用者環境へ波及しないものは、
上書き設定でピンを迂回し、迂回の理由と解除条件を該当箇所のコメントへ残す。

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
