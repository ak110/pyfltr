_PRESET_BASE: dict[str, bool] = {
    "ruff-format": True,
    "ruff-check": True,
    "mypy": True,
    "pylint": True,
    "arid": True,
    "pytest": True,
    "eslint": True,
    "biome": True,
    "oxlint": True,
    "prettier": True,
    "tsc": True,
    "vitest": True,
    "cargo-fmt": True,
    "cargo-clippy": True,
    "cargo-check": True,
    "cargo-test": True,
    "cargo-deny": True,
    "dotnet-format": True,
    "dotnet-build": True,
    "dotnet-test": True,
}

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
    "20260726": {
        **_PRESET_BASE,
        "pyright": True,
        "textlint": True,
        "markdownlint": True,
        "actionlint": True,
        "typos": True,
        "uv-sort": True,
        "prek": True,
    },
    "20260926": {
        **_PRESET_BASE,
        "pyright": True,
        "textlint": True,
        "markdownlint": True,
        "actionlint": True,
        "typos": True,
        "uv-sort": True,
        "prek": True,
        "pinact": True,
    },
}
_PRESETS["latest"] = _PRESETS["20260926"]

_REMOVED_PRESETS: dict[str, str] = {
    "20250710": (
        'preset "20250710" は v3.0.0 で削除された。'
        "5 ツール削除 (pyupgrade / autoflake / isort / black / pflake8) に伴い、"
        'このプリセットは実質的に内容を失ったため廃止された。代わりに `preset = "latest"` を使い、'
        "必要なPython系ツールを`python = true`または個別設定で有効化すること"
    ),
}


def get_preset(name: str) -> dict[str, bool] | None:
    preset = _PRESETS.get(name)
    return dict(preset) if preset is not None else None


def preset_names() -> tuple[str, ...]:
    return tuple(_PRESETS)


def get_removed_preset_message(name: str) -> str | None:
    return _REMOVED_PRESETS.get(name)
