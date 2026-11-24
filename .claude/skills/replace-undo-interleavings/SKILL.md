---
verified-digest: d123685969a0d2e41bbda80baaf688d347bf78e6de0adc0a3dcf916b3d8f8d41
name: replace-undo-interleavings
description: 一括置換とundoの履歴保存・書込み・巻戻しを変更するとき、障害と別の編集の順序を有界網羅検査で確かめる。
verifies:
  - pyfltr/grep_/operations.py
  - pyfltr/grep_/replacer.py
  - pyfltr/grep_/history.py
  - pyfltr/grep_/transaction.py
  - .claude/skills/replace-undo-interleavings/replace_undo_check.py
---

# 一括置換とundoの有界探索

## 性質と害

- 履歴の保存に失敗した置換は、対象を書き換えない。
- 書込みに失敗した置換・undoは、書き込んだファイルを開始前のバイト列へ戻すか、戻せなかったファイルを報告する。
  失敗した書込みの途中の内容と別の編集を区別できない場合は、内容を残して報告する。
- 巻戻し前にファイルを読み、別の編集で内容が変わっていたら、編集を残して競合を報告する。
  通常のundoは履歴のハッシュが一致しなければ全件をスキップする。
  強制undoは履歴のハッシュを比較しない。
  ただし、最初に読み取った内容と書込みの直前の内容が異なれば、書込みを中止する。

破ると複数ファイルに部分更新が残ったり、利用者が同時に保存した編集が消えたりする。

## 対象と範囲

`execute_replace`、`ReplaceHistoryStore.apply_replace`・`undo_replace`、`write_updates`を直接実行する。
対象ファイル2件のバイト列と一時履歴を使用する。
編集主体は1件だけを1回書く。
通常の別内容と、書込み予定の内容の途中までと同じバイト列を使う。
対象と履歴の入出力を`pathlib.Path`の呼出し単位で止め、`ThreadExplorer`で編集と対象処理の順序を列挙する。
1回の障害を順番に選んだ呼出しに注入する。
対象の書込みでは途中まで書いてから失敗する場合も調べる。

現在の内容が書込みの条件を満たすと確かめてから、実際に書き込むか削除するまでに入った編集は、そのファイルについてのみ保証しない。
プロセスの強制終了、電源断、複数の障害・編集、旧形式の履歴は対象外。

## 再実行

リポジトリルートで、開発依存の`pytilpack`を使って実行する。
通常のテストとは別に実行する。

```bash
uv run --frozen python .claude/skills/replace-undo-interleavings/replace_undo_check.py
```

終了コードと実行数・除外数・違反数を確認する。
違反時は操作名・障害・編集内容と手順の列が出る。
上のコマンドで性質を確かめた後にだけ記録を更新する。

```bash
uv run --frozen pyfltr verification-sync --update .claude/skills/replace-undo-interleavings/SKILL.md
```
