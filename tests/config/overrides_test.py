"""`pyfltr.config.overrides`のテスト。"""

import pathlib

import pytest

import pyfltr.config.config
import pyfltr.config.overrides
import pyfltr.warnings_

_GPU_SETTINGS = """
[tool.pyfltr]
env-file = ".env"
extend-exclude = ["build"]

[[tool.pyfltr.overrides]]
when = "not RUN_GPU_TESTS"
extend-exclude = ["tests/gpu"]
pytest-extend-args = ["-m", "not gpu"]
"""


def _write_project(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    (tmp_path / "pyproject.toml").write_text(body, encoding="utf-8")
    return tmp_path


def _load(tmp_path: pathlib.Path) -> pyfltr.config.config.Config:
    return pyfltr.config.config.load_config(tmp_path, global_config_path=tmp_path / "missing-global.toml")


@pytest.mark.parametrize("value", [None, "0", "false", "FALSE", " 0 ", ""])
def test_gpu_override_applies_when_variable_is_off(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """未定義・0・false・空では除外と追加引数が適用される。"""
    if value is None:
        monkeypatch.delenv("RUN_GPU_TESTS", raising=False)
    else:
        monkeypatch.setenv("RUN_GPU_TESTS", value)
    config = _load(_write_project(tmp_path, _GPU_SETTINGS))
    assert config.values["extend-exclude"] == ["build", "tests/gpu"]
    assert config.values["pytest-extend-args"] == ["-m", "not gpu"]


@pytest.mark.parametrize("value", ["1", "true", "True", " true "])
def test_gpu_override_is_skipped_when_variable_is_on(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """1・trueでは除外も追加引数も適用されない。"""
    monkeypatch.setenv("RUN_GPU_TESTS", value)
    config = _load(_write_project(tmp_path, _GPU_SETTINGS))
    assert config.values["extend-exclude"] == ["build"]
    assert config.values["pytest-extend-args"] == []


def test_env_file_value_is_used_and_process_value_wins(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env`だけに定義した値で判定し、プロセスの環境変数がある場合はプロセスの値を優先する。"""
    _write_project(tmp_path, _GPU_SETTINGS)
    (tmp_path / ".env").write_text("# comment\n\nexport RUN_GPU_TESTS='1'\nOTHER=x\n", encoding="utf-8")
    monkeypatch.delenv("RUN_GPU_TESTS", raising=False)
    assert _load(tmp_path).values["pytest-extend-args"] == []

    monkeypatch.setenv("RUN_GPU_TESTS", "0")
    assert _load(tmp_path).values["pytest-extend-args"] == ["-m", "not gpu"]


def test_env_file_missing_is_treated_as_empty(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """指定した`.env`が無い場合は警告せず空として扱う。"""
    monkeypatch.delenv("RUN_GPU_TESTS", raising=False)
    before = len(pyfltr.warnings_.collected_warnings())
    config = _load(_write_project(tmp_path, _GPU_SETTINGS))
    assert config.values["pytest-extend-args"] == ["-m", "not gpu"]
    assert len(pyfltr.warnings_.collected_warnings()) == before


def test_env_file_values_are_not_passed_to_subprocess_environment(tmp_path: pathlib.Path) -> None:
    """`.env`の値は`when`の判定にだけ使い、プロセスの環境変数へ書き込まない。"""
    _write_project(tmp_path, _GPU_SETTINGS)
    (tmp_path / ".env").write_text("PYFLTR_TEST_ONLY_IN_DOTENV=1\n", encoding="utf-8")
    _load(tmp_path)
    assert "PYFLTR_TEST_ONLY_IN_DOTENV" not in pyfltr.config.overrides.os.environ


def test_multiple_true_elements_concatenate_extend_keys_and_last_replacement_wins(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真の要素が複数あるとき、追加用のキーは全要素を連結し、置き換えのキーは後の要素の値になる。"""
    monkeypatch.setenv("A", "1")
    monkeypatch.setenv("B", "1")
    body = """
[tool.pyfltr]
pylint-args = ["--base"]

[[tool.pyfltr.overrides]]
when = "A"
extend-exclude = ["a"]
pylint-args = ["--from-a"]

[[tool.pyfltr.overrides]]
when = "A and (B or C)"
extend-exclude = ["b"]
pylint-args = ["--from-b"]
"""
    config = _load(_write_project(tmp_path, body))
    assert config.values["extend-exclude"] == ["a", "b"]
    assert config.values["pylint-args"] == ["--from-b"]


@pytest.mark.parametrize("when", ["A and", "(A", "A B", "A && B", "", "not"])
def test_invalid_when_is_warned_and_element_ignored(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, when: str) -> None:
    """構文が不正な`when`の要素は警告して無視し、他の要素と通常の設定は適用する。"""
    monkeypatch.delenv("A", raising=False)
    body = f"""
[tool.pyfltr]
extend-exclude = ["base"]

[[tool.pyfltr.overrides]]
when = "{when}"
extend-exclude = ["invalid"]

[[tool.pyfltr.overrides]]
when = "not A"
extend-exclude = ["valid"]
"""
    before = len(pyfltr.warnings_.collected_warnings())
    config = _load(_write_project(tmp_path, body))
    assert config.values["extend-exclude"] == ["base", "valid"]
    new_warnings = pyfltr.warnings_.collected_warnings()[before:]
    assert any("when" in w["message"] and "構文" in w["message"] for w in new_warnings)


def test_missing_when_and_forbidden_keys_are_warned(tmp_path: pathlib.Path) -> None:
    """`when`の無い要素は無視し、要素内の`overrides`・`env-file`・`custom-commands`はそのキーだけを無視する。"""
    body = """
[[tool.pyfltr.overrides]]
extend-exclude = ["no-when"]

[[tool.pyfltr.overrides]]
when = "not UNDEFINED_PYFLTR_TEST_VAR"
env-file = ".env.other"
extend-exclude = ["applied"]
"""
    before = len(pyfltr.warnings_.collected_warnings())
    config = _load(_write_project(tmp_path, body))
    assert config.values["extend-exclude"] == ["applied"]
    assert config.values["env-file"] == ""
    messages = [w["message"] for w in pyfltr.warnings_.collected_warnings()[before:]]
    assert any("`when`が無い" in m for m in messages)
    assert any("`env-file`を書けない" in m for m in messages)


def test_unknown_value_is_false_with_warning_without_value(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """1・true・0・false・空以外の値は偽と評価し、変数名を示す警告を出す（値は示さない）。"""
    monkeypatch.setenv("RUN_GPU_TESTS", "yes-secret")
    before = len(pyfltr.warnings_.collected_warnings())
    config = _load(_write_project(tmp_path, _GPU_SETTINGS))
    assert config.values["pytest-extend-args"] == ["-m", "not gpu"]
    messages = [w["message"] for w in pyfltr.warnings_.collected_warnings()[before:]]
    assert any("RUN_GPU_TESTS" in m for m in messages)
    assert not any("yes-secret" in m for m in messages)


def test_global_overrides_and_env_file_are_ignored_with_warning(tmp_path: pathlib.Path) -> None:
    """グローバル設定の`overrides`と`env-file`は警告して無視する。"""
    global_path = tmp_path / "global.toml"
    global_path.write_text(
        '[tool.pyfltr]\nenv-file = ".env"\n\n[[tool.pyfltr.overrides]]\nwhen = "not X"\nextend-exclude = ["g"]\n',
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    before = len(pyfltr.warnings_.collected_warnings())
    config = pyfltr.config.config.load_config(project, global_config_path=global_path)
    assert config.values["extend-exclude"] == []
    assert config.values["env-file"] == ""
    messages = [w["message"] for w in pyfltr.warnings_.collected_warnings()[before:]]
    assert any("グローバル設定の`overrides`" in m for m in messages)
    assert any("グローバル設定の`env-file`" in m for m in messages)


def test_project_without_overrides_is_unchanged(tmp_path: pathlib.Path) -> None:
    """`overrides`と`env-file`を書かないプロジェクトの設定は変わらない。"""
    body = '[tool.pyfltr]\nextend-exclude = ["x"]\n'
    config = _load(_write_project(tmp_path, body))
    assert config.values["extend-exclude"] == ["x"]
    assert config.values["env-file"] == ""


@pytest.mark.parametrize(
    ("expression", "env", "expected"),
    [
        ("A", {"A": "1"}, True),
        ("not A", {}, True),
        ("A or B and C", {"A": "0", "B": "1", "C": "1"}, True),
        ("A or B and C", {"A": "0", "B": "1", "C": "0"}, False),
        ("(A or B) and C", {"A": "1", "C": "0"}, False),
        ("not not A", {"A": "true"}, True),
        ("not A and not B", {"A": "0"}, True),
    ],
)
def test_when_precedence_matches_pytest_marker_expressions(expression: str, env: dict[str, str], expected: bool) -> None:
    """`not` > `and` > `or`の優先順位で評価する。"""
    expr = pyfltr.config.overrides.parse_when(expression)
    assert pyfltr.config.overrides.evaluate_when(expr, env, lambda key, message: None) is expected
