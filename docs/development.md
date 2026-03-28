# 開発手順

## 開発環境の構築手順

1. 本リポジトリをcloneする。
2. [uvをインストール](https://docs.astral.sh/uv/getting-started/installation/)する。
3. [pre-commit](https://pre-commit.com/)フックをインストールする。

    ```bash
    uv run pre-commit install
    ```

## ドキュメントサイト

ドキュメントはMkDocsで管理し、GitHub Pagesでホスティングしている。

### ローカルプレビュー

```bash
uv run mkdocs serve
```

<http://127.0.0.1:8000> でプレビューを確認できる。

### GitHub Pagesの設定

初回のみリポジトリの設定が必要。

1. GitHubのリポジトリ設定ページを開く。
2. Settings > Pages に移動する。
3. Source を「GitHub Actions」に設定する。

masterブランチへのpush時にdocs/配下やmkdocs.ymlの変更があると自動デプロイされる。

## リリース手順

事前に`gh`コマンドをインストールして`gh auth login`でログインしておき、以下のコマンドのいずれかを実行。

```bash
gh workflow run release.yaml --field="bump=バグフィックス"
gh workflow run release.yaml --field="bump=マイナーバージョンアップ"
gh workflow run release.yaml --field="bump=メジャーバージョンアップ"
```

<https://github.com/ak110/pyfltr/actions> で状況を確認できる。
