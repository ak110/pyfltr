import os
import pathlib

import platformdirs


def default_cache_root() -> pathlib.Path:
    override = os.environ.get("PYFLTR_CACHE_DIR")
    if override:
        return pathlib.Path(override)
    return pathlib.Path(platformdirs.user_cache_dir("pyfltr", appauthor=False))
