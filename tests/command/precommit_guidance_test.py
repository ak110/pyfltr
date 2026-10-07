import pathlib
import textwrap

import psutil
import pytest

import pyfltr.command.dispatcher
import pyfltr.command.precommit
import pyfltr.command.precommit_guidance
import pyfltr.command.process
import pyfltr.config.config
import pyfltr.config.model


class TestIsRunningUnderPrecommit:
    """`is_running_under_precommit`のテスト。"""

    def test_detects_pre_commit_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`PRE_COMMIT=1`でTrueを返す。"""
        monkeypatch.setenv("PRE_COMMIT", "1")
        assert pyfltr.command.precommit_guidance.is_running_under_precommit() is True

    def test_absence_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """未設定時はFalseを返す。"""
        monkeypatch.delenv("PRE_COMMIT", raising=False)
        assert pyfltr.command.precommit_guidance.is_running_under_precommit() is False

    def test_other_value_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ "1"以外の値はFalse扱い（pre-commit公式仕様に準拠）。"""
        monkeypatch.setenv("PRE_COMMIT", "0")
        assert pyfltr.command.precommit_guidance.is_running_under_precommit() is False


class _FakeProcess:
    """`psutil.Process` の最小スタブ。

    `name()` / `parents()` の挙動のみを制御する。実プロセスは参照しない。
    親系列は `__init__` に渡された順で返す。
    """

    def __init__(self, name: str, ancestors: list["_FakeProcess"] | None = None) -> None:
        self._name = name
        self._ancestors = ancestors or []

    def name(self) -> str:
        """プロセス名を返す（psutil.Process.name() 互換）。"""
        return self._name

    def parents(self) -> list["_FakeProcess"]:
        """祖先プロセスリストを返す（psutil.Process.parents() 互換）。"""
        return list(self._ancestors)


class TestIsInvokedFromGitCommit:
    """`is_invoked_from_git_commit`のテスト。"""

    def test_direct_parent_is_git(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """直接の親がgitの場合はTrueを返す。"""
        parent = _FakeProcess("git", ancestors=[])
        monkeypatch.setattr(pyfltr.command.precommit_guidance.psutil, "Process", lambda _pid: parent)
        assert pyfltr.command.precommit_guidance.is_invoked_from_git_commit() is True

    def test_ancestor_is_git(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """祖先にgitが含まれていればTrueを返す。"""
        ancestors = [
            _FakeProcess("pre-commit"),
            _FakeProcess("git"),
            _FakeProcess("bash"),
        ]
        parent = _FakeProcess("python", ancestors=ancestors)
        monkeypatch.setattr(pyfltr.command.precommit_guidance.psutil, "Process", lambda _pid: parent)
        assert pyfltr.command.precommit_guidance.is_invoked_from_git_commit() is True

    def test_windows_git_exe_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Windowsの`git.exe`も検出する。"""
        parent = _FakeProcess("git.exe", ancestors=[])
        monkeypatch.setattr(pyfltr.command.precommit_guidance.psutil, "Process", lambda _pid: parent)
        assert pyfltr.command.precommit_guidance.is_invoked_from_git_commit() is True

    def test_no_git_in_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """親系列にgitが無ければFalseを返す。"""
        ancestors = [_FakeProcess("bash"), _FakeProcess("sshd")]
        parent = _FakeProcess("python", ancestors=ancestors)
        monkeypatch.setattr(pyfltr.command.precommit_guidance.psutil, "Process", lambda _pid: parent)
        assert pyfltr.command.precommit_guidance.is_invoked_from_git_commit() is False

    def test_psutil_failure_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """psutilの取得失敗時は安全側に倒してFalseを返す。"""

        def _raise(_pid: int) -> psutil.Process:
            raise psutil.NoSuchProcess(pid=_pid)

        monkeypatch.setattr(pyfltr.command.precommit_guidance.psutil, "Process", _raise)
        assert pyfltr.command.precommit_guidance.is_invoked_from_git_commit() is False


class TestDetectPyfltrHooks:
    """detect_pyfltr_hooksのテスト。"""

    def test_single_pyfltr_entry(self, tmp_path: pathlib.Path) -> None:
        """単一のpyfltrエントリを検出する。"""
        config_path = tmp_path / ".pre-commit-config.yaml"
        config_path.write_text(
            textwrap.dedent("""\
                repos:
                  - repo: https://github.com/pre-commit/pre-commit-hooks
                    rev: v6.0.0
                    hooks:
                      - id: check-yaml
                  - repo: local
                    hooks:
                      - id: pyfltr
                        name: pyfltr
                        entry: uv run --frozen pyfltr fast
                        language: system
            """),
            encoding="utf-8",
        )
        result = pyfltr.command.precommit_guidance.detect_pyfltr_hooks(tmp_path)
        assert result == ["pyfltr"]

    def test_multiple_pyfltr_entries(self, tmp_path: pathlib.Path) -> None:
        """複数のpyfltrエントリを検出する。"""
        config_path = tmp_path / ".pre-commit-config.yaml"
        config_path.write_text(
            textwrap.dedent("""\
                repos:
                  - repo: local
                    hooks:
                      - id: pyfltr-app
                        entry: uv run pyfltr run --exit-zero-even-if-formatted --commands=fast app
                        language: system
                      - id: pyfltr-markdown
                        entry: uv run pyfltr run --exit-zero-even-if-formatted --commands=markdownlint,textlint
                        language: system
                      - id: pyfltr-server
                        entry: bash -c 'cd server && uv run pyfltr fast'
                        language: system
            """),
            encoding="utf-8",
        )
        result = pyfltr.command.precommit_guidance.detect_pyfltr_hooks(tmp_path)
        assert result == ["pyfltr-app", "pyfltr-markdown", "pyfltr-server"]

    def test_no_pyfltr_entry(self, tmp_path: pathlib.Path) -> None:
        """pyfltrエントリがない場合は空リストを返す。"""
        config_path = tmp_path / ".pre-commit-config.yaml"
        config_path.write_text(
            textwrap.dedent("""\
                repos:
                  - repo: https://github.com/pre-commit/pre-commit-hooks
                    rev: v6.0.0
                    hooks:
                      - id: check-yaml
                      - id: trailing-whitespace
            """),
            encoding="utf-8",
        )
        result = pyfltr.command.precommit_guidance.detect_pyfltr_hooks(tmp_path)
        assert not result

    def test_config_file_missing(self, tmp_path: pathlib.Path) -> None:
        """.pre-commit-config.yamlが存在しない場合は空リストを返す。"""
        result = pyfltr.command.precommit_guidance.detect_pyfltr_hooks(tmp_path)
        assert not result

    def test_empty_config(self, tmp_path: pathlib.Path) -> None:
        """空のYAMLファイルの場合は空リストを返す。"""
        config_path = tmp_path / ".pre-commit-config.yaml"
        config_path.write_text("", encoding="utf-8")
        result = pyfltr.command.precommit_guidance.detect_pyfltr_hooks(tmp_path)
        assert not result


class TestBuildSkipValue:
    """build_skip_valueのテスト。"""

    @pytest.fixture(name="config_with_auto_skip")
    def _config_with_auto_skip(self) -> pyfltr.config.model.Config:
        """auto-skip有効のConfig。"""
        config = pyfltr.config.config.create_default_config()
        config.values["pre-commit-auto-skip"] = True
        config.values["pre-commit-skip"] = []
        return config

    @pytest.fixture(name="config_without_auto_skip")
    def _config_without_auto_skip(self) -> pyfltr.config.model.Config:
        """auto-skip無効のConfig。"""
        config = pyfltr.config.config.create_default_config()
        config.values["pre-commit-auto-skip"] = False
        config.values["pre-commit-skip"] = []
        return config

    def test_auto_skip_detects_hooks(
        self,
        tmp_path: pathlib.Path,
        config_with_auto_skip: pyfltr.config.model.Config,
    ) -> None:
        """auto-skip有効時にpyfltr hookを検出してSKIP値に含める。"""
        config_path = tmp_path / ".pre-commit-config.yaml"
        config_path.write_text(
            textwrap.dedent("""\
                repos:
                  - repo: local
                    hooks:
                      - id: pyfltr
                        entry: uv run pyfltr fast
                        language: system
            """),
            encoding="utf-8",
        )
        result = pyfltr.command.precommit_guidance.build_skip_value(config_with_auto_skip, tmp_path, command="pre-commit")
        assert result == "pyfltr"

    def test_auto_skip_disabled(
        self,
        tmp_path: pathlib.Path,
        config_without_auto_skip: pyfltr.config.model.Config,
    ) -> None:
        """auto-skip無効時は自動検出しない。"""
        config_path = tmp_path / ".pre-commit-config.yaml"
        config_path.write_text(
            textwrap.dedent("""\
                repos:
                  - repo: local
                    hooks:
                      - id: pyfltr
                        entry: uv run pyfltr fast
                        language: system
            """),
            encoding="utf-8",
        )
        result = pyfltr.command.precommit_guidance.build_skip_value(config_without_auto_skip, tmp_path, command="pre-commit")
        assert result == ""

    def test_manual_skip_combined_with_auto(
        self,
        tmp_path: pathlib.Path,
        config_with_auto_skip: pyfltr.config.model.Config,
    ) -> None:
        """手動指定と自動検出を併用する。"""
        config_with_auto_skip.values["pre-commit-skip"] = ["manual-hook"]
        config_path = tmp_path / ".pre-commit-config.yaml"
        config_path.write_text(
            textwrap.dedent("""\
                repos:
                  - repo: local
                    hooks:
                      - id: pyfltr
                        entry: uv run pyfltr fast
                        language: system
            """),
            encoding="utf-8",
        )
        result = pyfltr.command.precommit_guidance.build_skip_value(config_with_auto_skip, tmp_path, command="pre-commit")
        assert result == "manual-hook,pyfltr"

    def test_manual_skip_no_duplicate(
        self,
        tmp_path: pathlib.Path,
        config_with_auto_skip: pyfltr.config.model.Config,
    ) -> None:
        """手動指定と自動検出で重複するIDは1つにする。"""
        config_with_auto_skip.values["pre-commit-skip"] = ["pyfltr"]
        config_path = tmp_path / ".pre-commit-config.yaml"
        config_path.write_text(
            textwrap.dedent("""\
                repos:
                  - repo: local
                    hooks:
                      - id: pyfltr
                        entry: uv run pyfltr fast
                        language: system
            """),
            encoding="utf-8",
        )
        result = pyfltr.command.precommit_guidance.build_skip_value(config_with_auto_skip, tmp_path, command="pre-commit")
        assert result == "pyfltr"

    def test_no_config_file(
        self,
        tmp_path: pathlib.Path,
        config_with_auto_skip: pyfltr.config.model.Config,
    ) -> None:
        """.pre-commit-config.yamlが存在しない場合は空文字を返す。"""
        result = pyfltr.command.precommit_guidance.build_skip_value(config_with_auto_skip, tmp_path, command="pre-commit")
        assert result == ""


class TestBuildPrekSkipValue:
    """prek設定を参照するbuild_skip_valueのテスト。"""

    def test_manual_skip_and_auto_skip_disabled(self, tmp_path: pathlib.Path) -> None:
        """prek-auto-skip無効時はprek-skipの手動指定だけを返す。"""
        config = pyfltr.config.config.create_default_config()
        config.values["prek-auto-skip"] = False
        config.values["prek-skip"] = ["manual-prek-hook"]

        result = pyfltr.command.precommit_guidance.build_skip_value(config, tmp_path, command="prek")

        assert result == "manual-prek-hook"

    def test_auto_skip_uses_prek_keys(self, tmp_path: pathlib.Path) -> None:
        """prek-auto-skip有効時はpyfltr hookを検出する。

        pre-commit側キーを対照値（auto-skip無効・手動指定あり）へ固定し、
        prek側キーのみが結果へ反映されることを判別できるようにする。
        """
        config = pyfltr.config.config.create_default_config()
        config.values["prek-auto-skip"] = True
        config.values["prek-skip"] = []
        config.values["pre-commit-auto-skip"] = False
        config.values["pre-commit-skip"] = ["should-not-appear"]
        (tmp_path / ".pre-commit-config.yaml").write_text(
            textwrap.dedent("""\
                repos:
                  - repo: local
                    hooks:
                      - id: pyfltr
                        entry: uv run --frozen pyfltr fast
                        language: system
            """),
            encoding="utf-8",
        )

        result = pyfltr.command.precommit_guidance.build_skip_value(config, tmp_path, command="prek")

        assert result == "pyfltr"
