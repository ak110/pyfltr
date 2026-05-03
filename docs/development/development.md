# 開発手順

## 開発環境の構築手順

1. 本リポジトリをcloneする
2. [uvをインストール](https://docs.astral.sh/uv/getting-started/installation/)する
3. 初回セットアップを実行する

    ```bash
    make setup
    ```

    依存のインストールとpre-commitフックの登録をまとめて行う

## 開発コマンド

本リポジトリでは「ローカル編集中のpyfltrを反映する」目的で`--with-editable=.`を使う。
他プロジェクト向けの利用者推奨は`uvx pyfltr ...`であり、本リポのみの限定例外として扱う。

```bash
# 全チェック実行（コミット前検証）
uv run --with-editable=. pyfltr run-for-agent

# 特定ファイルのみ実行
uv run --with-editable=. pyfltr run-for-agent path/to/file

# 特定ツールのみ絞り込んで実行
uv run --with-editable=. pyfltr run-for-agent --commands=mypy,ruff-check path/to/file
```

ドキュメントのみの変更の場合は省略可（pre-commitで実行されるため）。
テストコードの単体実行も極力pyfltrを使い、pytestを直接呼び出さない。
最終検証はCIに委ねる前提で、修正後の再実行時は対象を絞って実行する。

## サプライチェーン攻撃対策

サプライチェーン攻撃対策として`uvx`/`pnpx`用のグローバル設定をする（環境構築時に一度実行すれば十分）。

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

ドキュメントはMkDocsで管理し、GitHub Pagesでホスティングしている。

### ローカルプレビュー

```bash
uv run mkdocs serve
```

<http://127.0.0.1:8000> でプレビューを確認できる。

### mkdocs.yml編集時の注意

`mkdocs.yml`の`nav`を変更した場合は`uv run mkdocs build --strict`で
リンク切れや設定ミスがないことを確認する。
`llmstxt`プラグインの`sections`設定もnavに合わせて更新する。

### GitHub Pagesの設定

初回のみリポジトリの設定が必要。

1. GitHubのリポジトリ設定ページを開く
2. `Settings` → `Pages` に移動する
3. Sourceを「GitHub Actions」に設定する

masterブランチへのpush時にdocs/配下やmkdocs.ymlの変更があると自動デプロイされる。

## リリース手順

事前に`gh`コマンドをインストールして`gh auth login`でログインしておき、以下のコマンドのいずれかを実行。

```bash
gh workflow run release.yaml --field="bump=PATCH"
gh workflow run release.yaml --field="bump=MINOR"
gh workflow run release.yaml --field="bump=MAJOR"
```

<https://github.com/ak110/pyfltr/actions> で状況を確認できる。
