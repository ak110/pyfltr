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
上流パッケージの厳密ピンにより依存更新だけでは解消できない場合は`pyproject.toml`の
`[tool.uv] override-dependencies`でピンを迂回し、迂回の理由と解除条件を該当箇所のコメントへ残す。
この上書き設定は本リポジトリの依存解決にのみ適用され、配布物のメタデータには含まれない。
そのため`uvx`等で配布物を取得して依存を解決する利用者環境と、同じ方式でpyfltrを導入する公式Dockerイメージには
波及せず、利用者環境での解消には上流パッケージ側がピンを追従する必要がある。

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
