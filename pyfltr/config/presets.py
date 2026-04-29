"""プリセット定義。"""

# 全プリセットで共通の推奨ツール（Python核 + JavaScript / TypeScript + Rust + .NET）を
# 集約する。preset = "latest" + `{language} = true`だけで当該言語の推奨ツール一式が
# gateを通過して有効化される運用を実現するため、歴史的presetも含めて全バージョンに
# 同じ言語別推奨ツールを収録する。
# カテゴリキー（`python` / `javascript` / `rust` / `dotnet`）がgateとして働き、
# false（既定）の場合は該当ツールを最終的にFalseへ押し戻す。
# `ty`は本体がまだpreset収録レベルに達していないため除外し、利用側で個別に
# `ty = true`を指定する運用を維持する。
_PRESET_BASE: dict[str, bool] = {
    # Python 核
    "ruff-format": True,
    "ruff-check": True,
    "mypy": True,
    "pylint": True,
    "pytest": True,
    # JavaScript / TypeScript
    "eslint": True,
    "biome": True,
    "oxlint": True,
    "prettier": True,
    "tsc": True,
    "vitest": True,
    # Rust
    "cargo-fmt": True,
    "cargo-clippy": True,
    "cargo-check": True,
    "cargo-test": True,
    "cargo-deny": True,
    # .NET
    "dotnet-format": True,
    "dotnet-build": True,
    "dotnet-test": True,
}

# プリセット定義。各presetは`_PRESET_BASE`を基点に、日付時点で追加された
# Python / ドキュメント系の推奨ツールを差分として加えた全量スナップショット。
# `20250710`はv3.0.0で削除した（削除5ツール分の設定しか持たなかったため）。
_PRESETS: dict[str, dict[str, bool]] = {
    "20260330": {**_PRESET_BASE, "pyright": True, "textlint": True, "markdownlint": True},
    "20260411": {
        **_PRESET_BASE,
        "pyright": True,
        "textlint": True,
        "markdownlint": True,
        "actionlint": True,
        "typos": True,
        "uv-sort": True,
    },
    "20260413": {
        **_PRESET_BASE,
        "pyright": True,
        "textlint": True,
        "markdownlint": True,
        "actionlint": True,
        "typos": True,
        "uv-sort": True,
        "pre-commit": True,
    },
}
_PRESETS["latest"] = _PRESETS["20260413"]

# v3.0.0で削除されたプリセット名と、移行先を示すメッセージの対応表。
# `load_config`が該当プリセット指定を検知したら案内付きValueErrorを送出する。
_REMOVED_PRESETS: dict[str, str] = {
    "20250710": (
        'preset "20250710" は v3.0.0 で削除された。'
        "5 ツール削除 (pyupgrade / autoflake / isort / black / pflake8) に伴い、"
        '当該プリセットは実質的に内容を失ったため廃止された。代わりに `preset = "latest"` を使い、'
        "必要なPython系ツールを`python = true`または個別設定で有効化すること"
    ),
}
