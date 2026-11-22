import pyfltr.config.config
import pyfltr.config.selection


def test_builtin_command_order() -> None:
    config = pyfltr.config.config.create_default_config()

    selected = pyfltr.config.selection.resolve_aliases(["pytest", "ruff-check", "ruff-format"], config)

    assert selected == ["ruff-format", "ruff-check", "pytest"]
