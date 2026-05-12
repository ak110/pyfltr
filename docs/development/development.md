# 開発手順

## 開発環境の構築手順

1. 本リポジトリをcloneする
2. [uvをインストール](https://docs.astral.sh/uv/getting-started/installation/)する
3. 初回セットアップを実行する

    ```bash
    make setup
    ```

## 開発コマンド

| コマンド | 用途 |
| --- | --- |
| `make format` | 整形 + 軽量lint（開発時の手動実行用） |
| `make test` | 全チェック実行（コミット前に通過させる） |
| `make update` | 依存更新 + pre-commit autoupdate + アクション更新 + 全テスト |
| `uvx pyfltr run-for-agent` | エージェントからのチェック実行 |
| `uv run mkdocs serve` | ドキュメントのローカルプレビュー |

## サプライチェーン攻撃対策

ロック尊重・公開待機・ピン留め運用の3点を採用している。

- `uv.lock` を尊重するため `UV_FROZEN=1` を常時有効化している（Makefile・CI・pre-commitフック経由）。
  適用経路の詳細はMakefile側コメントに委ねる
- `uv.toml` の `exclude-newer` で公開直後パッケージを一定期間除外し、サプライチェーン汚染リスクを低減する
- GitHub Actionsのサードパーティアクションはハッシュピン留めで固定する（`make update-actions`で更新）

環境構築の初回に以下を実行する。

```bash
mkdir -p ~/.config/uv && echo 'exclude-newer = "1 day"' >> ~/.config/uv/uv.toml
pnpm config set minimum-release-age 1440 --global
```

## ドキュメントサイト運用

ドキュメントはMkDocsで管理し、GitHub Pagesでホスティングする。

### mkdocs.yml編集時の注意

`mkdocs.yml`の`nav`を変更した場合は`uv run mkdocs build --strict`で
リンク切れや設定ミスがないことを確認する。
`llmstxt`プラグインの`sections`設定もnavに合わせて更新する。

### GitHub Pagesの設定

初回のみリポジトリの設定が必要。

1. GitHubのリポジトリ設定ページを開く
2. `Settings` → `Pages` に移動する
3. Sourceを「GitHub Actions」に設定する

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
