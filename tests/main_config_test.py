"""`pyfltr config`サブコマンドのテストコード。

`tests/main_test.py`から分離した（pylintのtoo-many-lines対策）。
"""

import json
import os
import pathlib

import pytest

import pyfltr.cli.main
import pyfltr.cli.output_format
from tests import conftest as _testconf


class TestConfigSubcommand:
    """`pyfltr config`サブコマンドの統合テスト。

    `_isolate_global_config`fixture（autouse）で`PYFLTR_GLOBAL_CONFIG`は
    既にtmp配下のダミーパスへ固定されているため、`--global`時はそのパスが
    対象となる。project側は`monkeypatch.chdir(tmp_path)`でcwd配下の
    `pyproject.toml`が解決されるようにする。
    """

    def test_config_get_existing_key(self, monkeypatch, tmp_path, capsys) -> None:
        """project側で設定した値が`config get`で返る。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\narchive-max-age-days = 7\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "get", "archive-max-age-days"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "7"

    def test_config_get_default_value(self, monkeypatch, tmp_path, capsys) -> None:
        """未設定キーは`DEFAULT_CONFIG`の既定値が返る。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "get", "archive-max-age-days"])
        assert rc == 0
        # DEFAULT_CONFIGは30
        assert capsys.readouterr().out.strip() == "30"

    def test_config_get_unknown_key_errors(self, monkeypatch, tmp_path, capsys) -> None:
        """未知キーはexit 1。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "get", "unknown-key"])
        assert rc == 1
        assert "unknown-key" in capsys.readouterr().err

    def test_config_set_creates_pyproject_section(self, monkeypatch, tmp_path) -> None:
        """既存pyproject.tomlに対してsetが書き込み成功する（[tool.pyfltr]セクションが無くても）。"""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "set", "archive-max-age-days", "5"])
        assert rc == 0
        text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
        assert "[tool.pyfltr]" in text
        assert "archive-max-age-days = 5" in text

    def test_config_set_preserves_comments(self, monkeypatch, tmp_path) -> None:
        """既存pyproject.tomlのコメントが保持される（tomlkit効果の確認）。"""
        original = '[project]\nname = "demo"  # 重要なコメント\n\n[tool.pyfltr]\n# pyfltrのコメント\npreset = "latest"\n'
        (tmp_path / "pyproject.toml").write_text(original, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "set", "archive-max-age-days", "10"])
        assert rc == 0
        text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
        assert "# 重要なコメント" in text
        assert "# pyfltrのコメント" in text
        assert "archive-max-age-days = 10" in text

    def test_config_set_pyproject_missing_errors(self, monkeypatch, tmp_path, capsys) -> None:
        """pyproject不在ディレクトリでのsetはエラー終了。`--global`併用案内を含む。"""
        # tmp_pathにpyproject.tomlを生成しない
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "set", "archive-max-age-days", "5"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "pyproject.toml" in err
        assert "--global" in err

    def test_config_set_global_creates_file(self, monkeypatch, tmp_path) -> None:
        """`--global`指定時にglobal config.tomlが自動作成される。"""
        global_path = pathlib.Path(_get_global_config_env())
        # 既に存在しないことを確認
        assert not global_path.exists()
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "set", "--global", "archive-max-age-days", "5"])
        assert rc == 0
        assert global_path.exists()
        text = global_path.read_text(encoding="utf-8")
        assert "[tool.pyfltr]" in text
        assert "archive-max-age-days = 5" in text

    def test_config_set_warning_archive_in_project(self, monkeypatch, tmp_path) -> None:
        """archive-max-age-daysをproject側にsetすると警告が蓄積される。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "set", "archive-max-age-days", "5"])
        assert rc == 0
        assert _count_config_warnings("archive-max-age-days") == 1

    def test_config_set_warning_normal_in_global(self, monkeypatch, tmp_path) -> None:
        """js-runnerをglobal側にsetすると警告（archive/cache以外はproject優先）。"""
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "set", "--global", "js-runner", "npm"])
        assert rc == 0
        assert _count_config_warnings("js-runner") == 1

    def test_config_delete_existing_key(self, monkeypatch, tmp_path, capsys) -> None:
        """存在キーをdeleteで削除し、その後getすると既定値が返る。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\narchive-max-age-days = 5\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "delete", "archive-max-age-days"])
        assert rc == 0
        capsys.readouterr()
        rc = pyfltr.cli.main.run(["config", "get", "archive-max-age-days"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "30"

    def test_config_delete_missing_key(self, monkeypatch, tmp_path, capsys) -> None:
        """存在しないキーのdeleteはexit 0で終了。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "delete", "archive-max-age-days"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "archive-max-age-days" in out

    def test_config_list_text_format(self, monkeypatch, tmp_path, capsys) -> None:
        """textフォーマットで`key = value`形式が出力される。"""
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pyfltr]\narchive-max-age-days = 5\njs-runner = "pnpm"\n', encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "archive-max-age-days = 5" in out
        assert "js-runner = pnpm" in out

    def test_config_list_json_format(self, monkeypatch, tmp_path, capsys) -> None:
        """jsonフォーマットで`{"values": ...}`が出力される。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\narchive-max-age-days = 5\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "list", "--output-format", "json"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        data = json.loads(out)
        assert data == {"values": {"archive-max-age-days": 5}}

    @pytest.mark.parametrize("env_name", pyfltr.cli.output_format.AGENT_INDICATOR_ENVS)
    def test_config_list_agent_indicator_jsonl(self, env_name, monkeypatch, tmp_path, capsys) -> None:
        """エージェント検出変数のいずれかが設定されていれば、`config list`は--output-format未指定でもJSONLを出力する。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\narchive-max-age-days = 5\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(env_name, "1")
        rc = pyfltr.cli.main.run(["config", "list"])
        assert rc == 0
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0]) == {"key": "archive-max-age-days", "value": 5}

    def test_config_list_all_text_includes_defaults(self, monkeypatch, tmp_path, capsys) -> None:
        """`--all` text出力で既定値行に`(default)`が付き、明示値行には付かない。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\narchive-max-age-days = 5\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "list", "--all"])
        assert rc == 0
        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if line.strip()]
        # 明示値: (default)なし
        assert "archive-max-age-days = 5" in lines
        # 既定値の例: (default)付きで出力される（DEFAULT_CONFIGにあるキー）
        default_lines = [line for line in lines if line.endswith(" (default)")]
        assert len(default_lines) > 0
        # キー昇順
        keys = [line.split(" = ", 1)[0] for line in lines]
        assert keys == sorted(keys)

    def test_config_list_all_json_marks_default_per_key(self, monkeypatch, tmp_path, capsys) -> None:
        """`--all` json出力で各キーに`value`と`default`の2フィールドが付与される。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\narchive-max-age-days = 5\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "list", "--all", "--output-format", "json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out.strip())
        assert "values" in data
        values = data["values"]
        # 明示値
        assert values["archive-max-age-days"] == {"value": 5, "default": False}
        # 既定値の例（DEFAULT_CONFIGに存在し未明示のキー）
        defaults_marked = [v for v in values.values() if v["default"]]
        assert len(defaults_marked) > 0

    def test_config_list_all_jsonl_appends_default_field(self, monkeypatch, tmp_path, capsys) -> None:
        """`--all` jsonl出力で各行に`default`フィールドが追加される。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\narchive-max-age-days = 5\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "list", "--all", "--output-format", "jsonl"])
        assert rc == 0
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
        # 明示値行
        explicit = [rec for rec in lines if rec["key"] == "archive-max-age-days"]
        assert explicit == [{"key": "archive-max-age-days", "value": 5, "default": False}]
        # 既定値行が含まれる
        assert any(rec["default"] for rec in lines)
        # キー昇順
        keys = [rec["key"] for rec in lines]
        assert keys == sorted(keys)

    def test_config_list_all_empty_pyproject_marks_all_default(self, monkeypatch, tmp_path, capsys) -> None:
        """全キー既定（明示値なし）の場合、全行に`(default)`が付く。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "list", "--all"])
        assert rc == 0
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert lines
        assert all(line.endswith(" (default)") for line in lines)

    def test_config_set_unknown_key_errors(self, monkeypatch, tmp_path, capsys) -> None:
        """未知キーへのsetはexit 1。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "set", "unknown-key", "foo"])
        assert rc == 1
        assert "unknown-key" in capsys.readouterr().err

    def test_config_delete_unknown_key_errors(self, monkeypatch, tmp_path, capsys) -> None:
        """未知キーへのdeleteはexit 1。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "delete", "unknown-key"])
        assert rc == 1
        assert "unknown-key" in capsys.readouterr().err

    def test_config_unknown_subaction_errors(self, monkeypatch, tmp_path) -> None:
        """`pyfltr config`単独はargparseエラー（required=True）。"""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            pyfltr.cli.main.run(["config"])

    @pytest.mark.parametrize(
        "action,key,expect_suggestion",
        [
            # typoしきい値内 → サジェスト候補が並ぶ
            ("get", "python-runer", True),
            ("set", "python-runer", True),
            ("delete", "python-runer", True),
            # しきい値外 → 候補無し
            ("get", "totally-unrelated", False),
            ("set", "totally-unrelated", False),
            ("delete", "totally-unrelated", False),
        ],
    )
    def test_config_unknown_key_suggestion(
        self, monkeypatch, tmp_path, capsys, action: str, key: str, expect_suggestion: bool
    ) -> None:
        """`config get/set/delete`の未知キー文面にサジェスト・一覧確認手段が含まれる。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        argv = ["config", action, key]
        if action == "set":
            argv.append("foo")
        rc = pyfltr.cli.main.run(argv)
        assert rc == 1
        err = capsys.readouterr().err
        assert f"`{key}`" in err
        assert "pyfltr config list --all" in err
        if expect_suggestion:
            assert "もしかして:" in err
        else:
            assert "もしかして:" not in err


def _get_global_config_env() -> str:
    """`_isolate_global_config`fixtureが設定したglobal設定パスを返す。"""
    value = os.environ.get("PYFLTR_GLOBAL_CONFIG")
    assert value is not None, "PYFLTR_GLOBAL_CONFIG fixtureが機能していない"
    return value


# conftest.count_config_warningsを再エクスポート（同モジュール内の参照を統一するため）
_count_config_warnings = _testconf.count_config_warnings
