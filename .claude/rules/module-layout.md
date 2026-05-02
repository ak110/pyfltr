# pyfltrのモジュール構成方針

pyfltrのソースコードは`pyfltr/`直下に5つのサブパッケージ（`cli`・`command`・`config`・`output`・`state`）と
少数のトップレベルモジュール（`paths`・`warnings_`）で構成する。
サブパッケージごとの責務分離を維持し、命名は責務に沿わせる
（ガイダンス系は`cli`配下、実行系は`command`配下など）。
`__init__.py`ではre-exportせず、利用側はサブパッケージ内の具体モジュールから直接importする。
pyfltrはCLIツールであり、Pythonモジュールパスは内部実装として扱う。
内部リファクタリングではPython API互換性を維持しない。

サブパッケージ・モジュールごとの構成詳細とサブパッケージ間の依存方向は
[docs/development/architecture.md](../../docs/development/architecture.md#modules)を参照。
