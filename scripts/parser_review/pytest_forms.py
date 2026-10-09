"""pytest出力の解析レビューで確認する入力形態の生成定義。

形態の番号と確認対象は`.claude/agents/error-parser-reviewer.md`「pytest出力の解析を変更する場合の確認形態」に従う。
実出力を得られる形態はサンプルをpyfltr経由で実行して採取し、実行の途中終了や別の実行の連結のように
実出力の変換で生成する派生入力は、派生元の入力識別子と変換内容を記録する。
`--tb=auto`は専用サンプルの`[tool.pyfltr]`へ`pytest-tb-line = false`を書いて得るため、
レビュー対象リポジトリの設定を変えない。
"""

import collections.abc
import dataclasses
import json
import pathlib
import re
import typing

from scripts.parser_review import collect
from scripts.parser_review.samples import Sample

_CHILD_RUNNER = '''"""子プロセスでpytestを実行し、その出力を返す補助。"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run_child(*args, timeout=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTEST_", "COLUMNS"))}
    env["PYTHONUNBUFFERED"] = "1"
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args]
    try:
        completed = subprocess.run(command, cwd=HERE, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        return output.decode() if isinstance(output, bytes) else output
    return completed.stdout + completed.stderr
'''

_CHILD_FAIL = "def test_child():\n    assert 1 == 2\n"
_CHILD_EXIT = 'import os\n\n\ndef test_child_exit():\n    print("child output", flush=True)\n    os._exit(3)\n'
_CHILD_SLEEP = "import time\n\n\ndef test_child_sleep():\n    time.sleep(60)\n"
_CHILD_CAPTURED = 'def test_child_captured():\n    print("child captured line")\n    assert 1 == 2\n'
_CHILD_SAME_NAME = "def test_same_name():\n    assert 3 == 4\n"
_CHAINED = (
    'def test_chained():\n    try:\n        raise ValueError("inner " + "x" * 100)\n'
    '    except ValueError as exc:\n        raise RuntimeError("outer " + "y" * 100) from exc\n'
)
_LONG_MESSAGE = 'def test_long_message():\n    assert "a" * 200 == "b" * 200\n'


def _parent(body: str, *, name: str = "test_runs_child") -> str:
    return f"from child_runner import run_child\n\n\ndef {name}():\n{body}    assert 1 == 0\n"


@dataclasses.dataclass(frozen=True)
class Derivation:
    """実出力を変換して生成する派生入力。"""

    sources: tuple[str, ...]
    """派生元の形態ID。先頭の派生元の実行条件（cwd）を比較のパス解決条件に使う。"""
    transform: str
    """変換内容の説明。比較入力と採取記録へ残す。"""
    function: collections.abc.Callable[[list[str]], str]


@dataclasses.dataclass(frozen=True)
class Form:
    """確認形態の1入力。"""

    id: str
    number: int
    title: str
    sample: Sample | None = None
    derivation: Derivation | None = None


# 実行環境のpytestプラグイン（pytest-asyncio）が設定欠落の警告を出力へ混ぜ、形態の境界を崩さないよう設定を置く。
_PYPROJECT = '[tool.pytest.ini_options]\nasyncio_default_fixture_loop_scope = "function"\n'


def _real(form_id: str, number: int, title: str, files: dict[str, str], targets: tuple[str, ...], **kwargs: typing.Any) -> Form:
    sample = Sample(
        id=form_id, tool="pytest", files={"pyproject.toml": _PYPROJECT, **files}, targets=targets, description=title, **kwargs
    )
    return Form(id=form_id, number=number, title=title, sample=sample)


def _child_form(
    form_id: str, title: str, child_files: dict[str, str], parent_body: str, *, name: str = "test_runs_child"
) -> Form:
    files = {"child_runner.py": _CHILD_RUNNER, **child_files, "parent_test.py": _parent(parent_body, name=name)}
    return _real(form_id, 7, title, files, ("parent_test.py",))


_FINAL_SUMMARY_RE = re.compile(r"^=+ .* in \d+(?:\.\d+)?s.*=+$")


def truncate_before_final_summary(outputs: list[str]) -> str:
    """最終集計行とそれ以降を除き、親の実行が途中で終わった出力を生成する。"""
    lines = outputs[0].splitlines()
    indexes = [index for index, line in enumerate(lines) if _FINAL_SUMMARY_RE.match(line)]
    if not indexes:
        raise ValueError("派生元に最終集計行が無い")
    return "\n".join(lines[: indexes[-1]]) + "\n"


def append_following_run(outputs: list[str]) -> str:
    """親の出力の後へ、別の実行の開始部分（`collected`行まで）を連結する。"""
    parent, follower = outputs
    head: list[str] = []
    for line in follower.splitlines():
        head.append(line)
        if line.startswith("collected "):
            break
    if not any(line.startswith("=") and "test session starts" in line for line in head):
        raise ValueError("連結する実行に開始行が無い")
    return parent.rstrip("\n") + "\n" + "\n".join(head) + "\n"


_TWO_FAILURES = "def test_first():\n    assert 1 == 2\n\n\ndef test_second():\n    assert 3 == 4\n"
_CLASS_BASED = "class TestSample:\n    def test_method(self):\n        assert 1 == 2\n"
_WARNING_CONFTEST = (
    "import warnings\n\n\ndef pytest_terminal_summary(terminalreporter):\n"
    '    warnings.warn("late warning", DeprecationWarning, stacklevel=1)\n'
)
_INTERRUPT = "def test_a():\n    assert 1 == 2\n\n\ndef test_b():\n    raise KeyboardInterrupt\n"


def _width_forms() -> list[Form]:
    forms: list[Form] = []
    for columns in ("80", "81"):
        parity = "even" if int(columns) % 2 == 0 else "odd"
        for tb_label, settings in (("short", {}), ("auto", {"pytest-tb-line": False})):
            forms.append(
                _real(
                    f"pytest-08-columns-{parity}-tb-{tb_label}",
                    8,
                    f"集計行のメッセージの切り詰め（COLUMNS={columns}、--tb={tb_label}）",
                    {"long_test.py": _LONG_MESSAGE + "\n\n" + _CHAINED},
                    ("long_test.py",),
                    settings=settings,
                    env={"COLUMNS": columns},
                )
            )
    return forms


FORMS: tuple[Form, ...] = (
    _real(
        "pytest-01-param-ids",
        1,
        "パラメータ化テストのIDに空白・ハイフン区切り・閉じ角括弧・入れ子の角括弧を含む形",
        {
            "param_test.py": 'import pytest\n\n\n@pytest.mark.parametrize(\n    "value",\n'
            '    ["a b", "x-y", "end]", "[nested]"],\n'
            '    ids=["with space", "hyphen-sep", "close]bracket", "nest[ed]"],\n)\n'
            'def test_param(value):\n    assert value == "never"\n'
        },
        ("param_test.py",),
    ),
    _real("pytest-02-class-based", 2, "クラスベースのテスト", {"class_test.py": _CLASS_BASED}, ("class_test.py",)),
    _real(
        "pytest-03-xdist-crash-function",
        3,
        "並列実行時のワーカー異常終了（関数テスト）",
        {"crash_test.py": "import os\n\n\ndef test_crash():\n    os._exit(1)\n\n\ndef test_fail():\n    assert 1 == 2\n"},
        ("crash_test.py",),
        settings={"pytest-args": ["-n", "2"]},
    ),
    _real(
        "pytest-04-xdist-crash-class",
        4,
        "並列実行時のワーカー異常終了（クラスベースのテスト）",
        {
            "crash_test.py": "import os\n\n\nclass TestCrash:\n    def test_crash(self):\n        os._exit(1)\n\n"
            "    def test_fail(self):\n        assert 1 == 2\n"
        },
        ("crash_test.py",),
        settings={"pytest-args": ["-n", "2"]},
    ),
    _real(
        "pytest-05-external-frame",
        5,
        "フレーム選択がプロジェクト外フレームへフォールバックする失敗",
        {"external_test.py": 'import json\n\n\ndef test_external():\n    json.loads("{")\n'},
        ("external_test.py",),
    ),
    _real(
        "pytest-06-same-name-files",
        6,
        "同名テストが複数ファイルにある場合",
        {"one_test.py": "def test_same():\n    assert 1 == 2\n", "two_test.py": "\n\ndef test_same():\n    assert 3 == 4\n"},
        ("one_test.py", "two_test.py"),
    ),
    _child_form(
        "pytest-07-child-tb-short",
        "子プロセスが正常に終了する形（--tb=short）",
        {"child/child_cases.py": _CHILD_FAIL},
        '    print(run_child("--tb=short", "child/child_cases.py"))\n',
    ),
    _child_form(
        "pytest-07-child-tb-line",
        "子プロセスが正常に終了する形（--tb=line）",
        {"child/child_cases.py": _CHILD_FAIL},
        '    print(run_child("--tb=line", "child/child_cases.py"))\n',
    ),
    _child_form(
        "pytest-07-child-os-exit",
        "子プロセスが終了集計行を欠く形（os._exitでの異常終了）",
        {"child/child_cases.py": _CHILD_EXIT},
        '    print(run_child("--tb=short", "child/child_cases.py"))\n',
    ),
    _child_form(
        "pytest-07-child-timeout",
        "子プロセスが終了集計行を欠く形（subprocess.runのtimeout打ち切り）",
        {"child/child_cases.py": _CHILD_SLEEP},
        '    print(run_child("--tb=short", "child/child_cases.py", timeout=5))\n',
    ),
    _child_form(
        "pytest-07-child-twice-first-unterminated",
        "子プロセスの実行が2回以上現れ、1回目が終了集計行を欠く形",
        {"child/exit_cases.py": _CHILD_EXIT, "child/fail_cases.py": _CHILD_FAIL},
        '    print(run_child("--tb=short", "child/exit_cases.py"))\n'
        '    print(run_child("--tb=short", "child/fail_cases.py"))\n',
    ),
    _child_form(
        "pytest-07-child-tail-only",
        "未終端の実行の後に、開始行を持たない終了集計行が現れる形（子の出力の末尾だけを表示）",
        {"child/exit_cases.py": _CHILD_EXIT, "child/fail_cases.py": _CHILD_FAIL},
        '    print(run_child("--tb=short", "child/exit_cases.py"))\n'
        '    print("\\n".join(run_child("--tb=short", "child/fail_cases.py").splitlines()[-3:]))\n',
    ),
    _child_form(
        "pytest-07-child-nested-captured",
        "子プロセス側の失敗したテストが出力を持ち、捕捉出力の節見出しが入れ子で現れる形",
        {"child/child_cases.py": _CHILD_CAPTURED},
        '    print(run_child("--tb=short", "child/child_cases.py"))\n',
    ),
    _child_form(
        "pytest-07-child-quiet",
        "子プロセスを静音モードで起動した形（-q）",
        {"child/child_cases.py": _CHILD_FAIL},
        '    print(run_child("-q", "--tb=short", "child/child_cases.py"))\n',
    ),
    _child_form(
        "pytest-07-child-same-name",
        "親子で同名のテストが失敗する形",
        {"child/child_cases.py": _CHILD_SAME_NAME},
        '    print(run_child("--tb=short", "child/child_cases.py"))\n',
        name="test_same_name",
    ),
    _real(
        "pytest-07-no-child-run",
        7,
        "捕捉出力が子プロセスの実行を含まない形（除外が働かないことの確認）",
        {
            "plain_test.py": 'def test_with_output():\n    print("plain captured line")\n    assert 1 == 2\n\n\n'
            "def test_after():\n    assert 3 == 4\n"
        },
        ("plain_test.py",),
    ),
    *_width_forms(),
    _real(
        "pytest-09-no-summary-list",
        9,
        "親が失敗一覧を持たない形（-rN）",
        {"fail_test.py": _TWO_FAILURES},
        ("fail_test.py",),
        settings={"pytest-args": ["-rN"]},
    ),
    _real(
        "pytest-10-quiet-parent",
        10,
        "親を静音モードで起動した形（-q）",
        {"fail_test.py": _TWO_FAILURES},
        ("fail_test.py",),
        settings={"pytest-args": ["-q"]},
    ),
    _real(
        "pytest-11-tb-auto-single-frame",
        11,
        "既定のトレースバック形式: フレームが1つだけの失敗（assert直書き）",
        {"auto_test.py": "def test_single():\n    assert 1 == 2\n"},
        ("auto_test.py",),
        settings={"pytest-tb-line": False},
    ),
    _real(
        "pytest-11-tb-auto-chained",
        11,
        "既定のトレースバック形式: 例外の連鎖",
        {"auto_test.py": _CHAINED},
        ("auto_test.py",),
        settings={"pytest-tb-line": False},
    ),
    _real(
        "pytest-11-tb-auto-mixed-frames",
        11,
        "既定のトレースバック形式: 詳細形式と簡略形式のエントリーが混在する多段フレームの失敗",
        {
            "auto_test.py": "def level3(value):\n    assert value == 0\n\n\ndef level2(value):\n    level3(value)\n\n\n"
            "def level1(value):\n    level2(value)\n\n\ndef test_mixed():\n    level1(1)\n"
        },
        ("auto_test.py",),
        settings={"pytest-tb-line": False},
    ),
    _real(
        "pytest-11-tb-auto-external-last",
        11,
        "既定のトレースバック形式: 最後のエントリーがプロジェクト外で終わる失敗",
        {
            "auto_test.py": 'import json\n\n\ndef helper():\n    return json.loads("{")\n\n\n'
            "def test_external():\n    helper()\n"
        },
        ("auto_test.py",),
        settings={"pytest-tb-line": False},
    ),
    Form(
        "pytest-12-truncated-with-summary-list",
        12,
        "親の実行が途中で終わり最終集計行を欠く形（失敗一覧を持つ場合）",
        derivation=Derivation(
            ("pytest-02-class-based",),
            "pytest-02-class-based の出力から最終集計行とそれ以降を削除",
            truncate_before_final_summary,
        ),
    ),
    Form(
        "pytest-12-truncated-without-summary-list",
        12,
        "親の実行が途中で終わり最終集計行を欠く形（失敗一覧を持たない場合）",
        derivation=Derivation(
            ("pytest-09-no-summary-list",),
            "pytest-09-no-summary-list の出力から最終集計行とそれ以降を削除",
            truncate_before_final_summary,
        ),
    ),
    _real(
        "pytest-13-parent-tb-no",
        13,
        "形態13の派生元: トレースバックを出力しない設定の親（--tb=no）",
        {"fail_test.py": _TWO_FAILURES},
        ("fail_test.py",),
        settings={"pytest-args": ["--tb=no"]},
    ),
    Form(
        "pytest-13-following-run-with-traceback",
        13,
        "親の実行の後に別の実行の出力が続く形（トレースバックを出力する設定）",
        derivation=Derivation(
            ("pytest-02-class-based", "pytest-06-same-name-files"),
            "pytest-02-class-based の出力の後へ pytest-06-same-name-files の出力の開始部分（collected行まで）を連結",
            append_following_run,
        ),
    ),
    Form(
        "pytest-13-following-run-without-traceback",
        13,
        "親の実行の後に別の実行の出力が続く形（トレースバックを出力しない設定）",
        derivation=Derivation(
            ("pytest-13-parent-tb-no", "pytest-06-same-name-files"),
            "pytest-13-parent-tb-no の出力の後へ pytest-06-same-name-files の出力の開始部分（collected行まで）を連結",
            append_following_run,
        ),
    ),
    _real(
        "pytest-14-warnings-after-summary",
        14,
        "失敗一覧より後に警告の集計が続く形",
        {"conftest.py": _WARNING_CONFTEST, "w_test.py": "def test_w():\n    assert 1 == 2\n"},
        ("w_test.py",),
    ),
    _real(
        "pytest-14-interrupt-after-summary",
        14,
        "失敗一覧より後に中断の報告が続く形（--full-trace）",
        {"k_test.py": _INTERRUPT},
        ("k_test.py",),
        settings={"pytest-args": ["--full-trace"]},
    ),
)


def select_forms(selectors: list[str] | None) -> list[Form]:
    """形態番号または形態IDで選び、派生入力の派生元も含めて定義順で返す。"""
    by_id = {form.id: form for form in FORMS}
    if selectors is None:
        chosen = set(by_id)
    else:
        chosen = set()
        for selector in selectors:
            if selector in by_id:
                chosen.add(selector)
            elif selector.isdigit() and any(form.number == int(selector) for form in FORMS):
                chosen.update(form.id for form in FORMS if form.number == int(selector))
            else:
                raise ValueError(f"未知の形態です: {selector}")
    for form_id in list(chosen):
        derivation = by_id[form_id].derivation
        if derivation is not None:
            chosen.update(derivation.sources)
    return [form for form in FORMS if form.id in chosen]


def derive(output_dir: pathlib.Path, form: Form, records: dict[str, dict[str, typing.Any]]) -> dict[str, typing.Any]:
    """派生元の採取記録から派生入力を生成し、採取記録と同じ形の記録を返す。"""
    derivation = form.derivation
    assert derivation is not None
    record: dict[str, typing.Any] = {
        "id": form.id,
        "tool": "pytest",
        "description": form.title,
        "has_parse_definition": True,
        "derived_from": list(derivation.sources),
        "transform": derivation.transform,
    }
    sources = [records.get(source_id) for source_id in derivation.sources]
    if any(source is None or source.get("state") != collect.STATE_COMPARABLE for source in sources):
        record["state"] = collect.STATE_NOT_COLLECTED
        record["reason"] = "派生元を採取できなかった"
        return record
    outputs = [pathlib.Path(typing.cast(dict, source)["output"]).read_text(encoding="utf-8") for source in sources]
    try:
        derived = derivation.function(outputs)
    except ValueError as exc:
        record["state"] = collect.STATE_NOT_COLLECTED
        record["reason"] = f"変換できなかった: {exc}"
        return record
    work_dir = output_dir / form.id
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = work_dir / "output.log"
    output_path.write_text(derived, encoding="utf-8")
    first = typing.cast(dict, sources[0])
    record.update({"cwd": first["cwd"], "output": str(output_path), "state": collect.STATE_COMPARABLE})
    return record


def generate(collector: collect.Collector, forms: list[Form]) -> list[dict[str, typing.Any]]:
    """選んだ形態を採取・派生し、定義順の記録を返す。"""
    collector.output_dir.mkdir(parents=True, exist_ok=True)
    records: dict[str, dict[str, typing.Any]] = {}
    for form in forms:
        if form.sample is not None:
            record = collect.collect_sample(collector, form.sample)
            record["form"] = form.number
            records[form.id] = record
    for form in forms:
        if form.derivation is not None:
            record = derive(collector.output_dir, form, records)
            record["form"] = form.number
            records[form.id] = record
    return [records[form.id] for form in forms]


def catalog() -> list[dict[str, typing.Any]]:
    """形態の一覧（番号・ID・説明・派生元）を返す。"""
    return [
        {
            "number": form.number,
            "id": form.id,
            "title": form.title,
            **(
                {"derived_from": list(form.derivation.sources), "transform": form.derivation.transform}
                if form.derivation
                else {}
            ),
        }
        for form in FORMS
    ]


def dump_catalog() -> str:
    """形態の一覧をJSON文字列で返す。"""
    return json.dumps(catalog(), ensure_ascii=False, indent=2)
