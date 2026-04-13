"""pre-commit統合の処理。"""

import logging
import pathlib

import yaml

import pyfltr.config

logger = logging.getLogger(__name__)


def detect_pyfltr_hooks(config_dir: pathlib.Path) -> list[str]:
    """pre-commit-config.yamlからpyfltr関連hookのIDを検出する。

    entryフィールドに"pyfltr"を含むhookのIDを返す。
    複数のpyfltrエントリ（pyfltr-app、pyfltr-markdown等）にも対応する。
    """
    config_path = config_dir / ".pre-commit-config.yaml"
    if not config_path.exists():
        return []

    with config_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        return []

    hook_ids: list[str] = []
    for repo in data.get("repos", []):
        if not isinstance(repo, dict):
            continue
        for hook in repo.get("hooks", []):
            if not isinstance(hook, dict):
                continue
            entry = hook.get("entry", "")
            if isinstance(entry, str) and "pyfltr" in entry:
                hook_id = hook.get("id", "")
                if hook_id:
                    hook_ids.append(hook_id)
    return hook_ids


def build_skip_value(config: pyfltr.config.Config, config_dir: pathlib.Path) -> str:
    """SKIP環境変数に渡す値を構築する。

    auto-skipが有効なら自動検出結果を、手動指定と合わせてカンマ区切りで返す。
    """
    skip_ids: list[str] = list(config["pre-commit-skip"])
    if config["pre-commit-auto-skip"]:
        auto_ids = detect_pyfltr_hooks(config_dir)
        for hook_id in auto_ids:
            if hook_id not in skip_ids:
                skip_ids.append(hook_id)
    if skip_ids:
        logger.debug("pre-commit SKIP対象: %s", ", ".join(skip_ids))
    return ",".join(skip_ids)
