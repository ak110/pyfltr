# 開発手順

## 開発環境の構築手順

1. 本リポジトリをcloneする
2. [uvをインストール](https://docs.astral.sh/uv/getting-started/installation/)する
3. [pre-commit](https://pre-commit.com/)フックをインストールする

    ```bash
    uv run pre-commit install
    ```

4. サプライチェーン攻撃対策として`uvx`/`pnpx`用のグローバル設定をする

    ```bash
    mkdir -p ~/.config/uv && echo 'exclude-newer = "1 day"' >> ~/.config/uv/uv.toml
    pnpm config set minimum-release-age 1440 --global
    ```

## UV_FROZENによるlockfile尊重（サプライチェーン攻撃対策）

CI/`make`などの自動実行環境で`uv sync`/`uv run`が依存解決を再実行せず`uv.lock`をそのまま使うよう、環境変数`UV_FROZEN=1`を常時有効化している。
意図しない再resolveでロックファイルが書き換わるリスクを抑え、`pyproject.toml`の`exclude-newer = "1 day"`と組み合わせて二重防御として機能する。

- `make format`や`make test`は`Makefile`の`export UV_FROZEN := 1`で自動適用される
- CIは`uv sync`/`uv run`を実行するworkflow（`ci.yaml`/`docs.yaml`）の`env.UV_FROZEN`で自動適用される。`release.yaml`は`uv build`/`uv publish`しか使わず、これらは`UV_FROZEN`の対象外なので設定不要
- `git commit`経由のpre-commitフックは`.pre-commit-config.yaml`のlocal hookのentryに`--frozen`を明示している

開発者のシェルでは`UV_FROZEN`を設定しない前提なので、依存の追加・更新は通常どおり`uv add`/`uv remove`/`uv lock --upgrade-package`を使えばよい。
`make update`も内部で自動的にUV_FROZENを外すため、そのまま実行してよい。

## ドキュメント

ドキュメントはMkDocsで管理し、GitHub Pagesでホスティングしている。

### ローカルプレビュー

```bash
uv run mkdocs serve
```

<http://127.0.0.1:8000> でプレビューを確認できる。

### GitHub Pagesの設定

初回のみリポジトリの設定が必要。

1. GitHubのリポジトリ設定ページを開く
2. `Settings` → `Pages` に移動する
3. Sourceを「GitHub Actions」に設定する

masterブランチへのpush時にdocs/配下やmkdocs.ymlの変更があると自動デプロイされる。

## リリース手順

事前に`gh`コマンドをインストールして`gh auth login`でログインしておき、以下のコマンドのいずれかを実行。

```bash
gh workflow run release.yaml --field="bump=バグフィックス"
gh workflow run release.yaml --field="bump=マイナーバージョンアップ"
gh workflow run release.yaml --field="bump=メジャーバージョンアップ"
```

<https://github.com/ak110/pyfltr/actions> で状況を確認できる。
