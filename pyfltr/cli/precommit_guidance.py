"""pre-commit統合の処理。"""

import logging
import os
import pathlib

import psutil
import yaml

import pyfltr.config.config

logger = logging.getLogger(__name__)


def is_running_under_precommit() -> bool:
    """pre-commit配下で実行されているかを判定する。

    pre-commitフレームワークは子プロセスへ`PRE_COMMIT=1`を設定する。
    これを検出して、pyfltr側のpre-commit統合を自動スキップする判断に使う。
    """
    return os.environ.get("PRE_COMMIT") == "1"


_GIT_PROCESS_NAMES: frozenset[str] = frozenset({"git", "git.exe"})


def is_invoked_from_git_commit() -> bool:
    """親プロセス系列にgitコマンドが居るかを判定する。

    pre-commitは`git commit`がspawnする`git-hook` → `pre-commit` → `pyfltr`
    という親子関係で動くため、祖先プロセスに`git`が含まれればgit commit経由の
    起動と判断できる。formatterによる自動修正が発生したときに、ユーザーへ
    「git addしてからcommitし直す」ガイダンスを出す条件として使う。

    psutilの取得に失敗した場合（`NoSuchProcess` / `AccessDenied`や
    プラットフォーム未対応）は`False`を返し、安全側に倒す（誤ったガイダンスを
    表示しない）。
    """
    try:
        proc = psutil.Process(os.getppid())
        # parents() は自身を含まないため、直接の親を判定対象に含めるよう明示的に先頭に付ける。
        candidates = [proc, *proc.parents()]
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    for candidate in candidates:
        try:
            name = candidate.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if name in _GIT_PROCESS_NAMES:
            return True
    return False


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


def build_skip_value(config: pyfltr.config.config.Config, config_dir: pathlib.Path) -> str:
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
