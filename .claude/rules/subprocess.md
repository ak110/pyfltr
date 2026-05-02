# pyfltrのsubprocess起動時のPATH整理方針

pyfltrのCLI起動時に`os.environ["PATH"]`を順序先勝ちで重複排除して書き戻す。
これによりプロセス内で起動する全subprocessへ自動的に波及する。
書き換え位置は`pyfltr/cli/main.py`の`main()`冒頭で、ライブラリ用途では実行されない。
重複排除の比較キーはOS依存に正規化する。
Windowsは大文字と小文字を区別せず、`/`と`\\`を等価に扱う。
POSIXは末尾スラッシュのみ落とす。
Windowsの`Path` / `PATH`揺れは検出したキー名のまま書き戻す。

mise経由のsubprocess（`bin-runner = "mise"`等で起動するもの）に限り、PATHから「miseが注入したtoolパス」を除外したenvを渡す。
対象パスは`mise/installs/`配下・`mise/dotnet-root`・`mise/shims`を含むエントリ。
判定ロジックは`pyfltr/command/env.py`の`build_subprocess_env`が担う。
判定値は`ensure_mise_available`通過後の`ResolvedCommandline.effective_runner`を使う。
mise不在時のdirectフォールバック後の値で判断するため、`build_commandline`直後の値は採用しない。
`ensure_mise_available`内の`mise exec --version` / `mise trust`にも同じ除外envを明示的に渡す。
mise本体のバイナリディレクトリ（`mise/bin`を含むエントリ）は保護対象として除外しない。

本対応はmise側の挙動への対症療法である。
親PATHにmise自身のtoolエントリを見つけると、miseはtools解決をスキップしてPATH解決にフォールバックする。
mise側の修正後は本対応の撤去または維持を再検討する余地がある。
