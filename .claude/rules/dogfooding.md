---
paths:
  - "pyfltr/command/builtin.py"
  - "pyfltr/config/config.py"
  - "Makefile"
  - ".pre-commit-config.yaml"
  - ".github/workflows/**"
  - ".gitlab-ci.yml"
---

# pyfltrのドッグフーディング方針

pyfltr自身のリポジトリでは対応ツールを可能な限り有効化し、動作確認とサンプル設定の提示を兼ねる。
新ツールを追加した際は本プロジェクトでも合わせて有効化することを既定とし、毎回の判断は不要とする。

対象外とするのは次のいずれかに該当する場合のみ。

- 入力ファイルが本リポジトリに存在しないツール（例: `*.svelte`が無い状態でのsvelte-check）
- ネットワーク・認証等の外部依存によりCI安定度を著しく下げるツール（既定でdisable運用しているものを含む）
