# 開発手順

## パッケージ管理

- [uv](https://docs.astral.sh/uv/)を使用してパッケージ管理を行う。

## pre-commit

- [pre-commit](https://pre-commit.com/)を使用してコミット時にコードの整形・チェックを行う。
- `pre-commit install`で有効化する。

## リリース手順

事前に`gh`コマンドをインストールし、`gh auth login`でログインしておく。

1. 現在のバージョンの確認:
   `git fetch --tags && git tag --sort=version:refname | tail -n1`
2. GitHubでリリースを作成する。
   `gh release create --target=master --generate-notes v1.x.x`
