# 開発手順

## 開発環境の構築手順

1. 本リポジトリをcloneする
2. [uvをインストール](https://docs.astral.sh/uv/getting-started/installation/)する
3. 初回セットアップを実行する

    ```bash
    make setup
    ```

    依存のインストールとpre-commitフックの登録をまとめて実行する

## 開発コマンド

```bash
make format   # 整形 + 軽量lint + 自動修正（開発時の手動実行用）
make test     # 全チェック実行（これを通過すればコミット可能）
make update   # 依存更新
```

### スモークテスト

`tests/smoke_test.py`は対応ツール群を実起動して終了確認するスモークテスト。
pnpm 11の`enableGlobalVirtualStore`既定変更のような外部ツール側の挙動変化を、
コマンドライン組立の単体テストでは捕捉できないためCI上で早期検出する目的で導入する。

- ローカル実行時、対象ツールが未インストールの場合は当該ケースをスキップする
- CI実行時（環境変数`CI`が設定されているとき）は失敗扱いとし、ツール群の同梱抜けを検知する
- `cargo`系・`dotnet`系は重量級のため除外、`glab-ci-lint`はネットワーク・認証依存のため除外する
- スモークケースのみを実行する場合: `uv run pytest tests/smoke_test.py -m smoke`

## サプライチェーン攻撃対策

サプライチェーン攻撃対策として`uvx`/`pnpx`用のグローバル設定を環境構築時に一度実行する。

```bash
mkdir -p ~/.config/uv && echo 'exclude-newer = "1 day"' >> ~/.config/uv/uv.toml
pnpm config set minimum-release-age 1440 --global
```

CI/`make`などの自動実行環境では環境変数`UV_FROZEN=1`を常時有効化している。
`uv sync`/`uv run`が依存解決を再実行せず`uv.lock`をそのまま使うようにするためである。
意図しない再resolveでロックファイルが書き換わるリスクを抑え、`pyproject.toml`の`exclude-newer = "1 day"`と
組み合わせて二重防御として機能する。

- `make format`/`make test`/`make setup`は`Makefile`の`export UV_FROZEN := 1`で自動適用される
- CIは`.github/workflows/*.yaml`の`env.UV_FROZEN`で自動適用される
- `git commit`経由のpre-commitフックは`.pre-commit-config.yaml`のlocal hookのentryに`--frozen`を明示している

開発者のシェルでは`UV_FROZEN`を設定しない前提なので、依存の追加・更新は通常どおり
`uv add`/`uv remove`/`uv lock --upgrade-package`を使えばよい。
`make update`も内部で自動的にUV_FROZENを外すため、そのまま実行してよい。

## ドキュメントサイト運用

ドキュメントはMkDocsで管理し、GitHub Pagesでホスティングする。

### ローカルプレビュー

```bash
uv run mkdocs serve
```

<http://127.0.0.1:8000> でプレビューを確認する。

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
