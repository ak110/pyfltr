# 開発手順

## 開発環境構築手順

1. 本リポジトリをcloneする。
2. [uvをインストール](https://docs.astral.sh/uv/getting-started/installation/)する。
3. [pre-commit](https://pre-commit.com/)フックをインストールする。

    ```bash
    uv run pre-commit install
    ```

## リリース手順

事前に`gh`コマンドをインストールして`gh auth login`でログインしておき、以下のコマンドのいずれかを実行。

```bash
gh workflow run release.yml --field="bump=バグフィックス"
gh workflow run release.yml --field="bump=マイナーバージョンアップ"
gh workflow run release.yml --field="bump=メジャーバージョンアップ"
```

<https://github.com/ak110/pyfltr/actions> で状況を確認できる。
