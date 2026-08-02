import json
import pathlib
import re
import subprocess
import sys

import pytest
import tomlkit

import pyfltr.command.error_parser
from scripts import check_doc_codeblocks

ERROR_PATTERN = re.compile(r"(?P<file>.+):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+)")
PROJECT_ROOT = pathlib.Path(__file__).parent.parent

FENCE_CASES = [
    pytest.param("```toml\nkey = 1\n```\n", "toml", 1, "key = 1\n", id="backticks"),
    pytest.param("````json\n{}\n````\n", "json", 1, "{}\n", id="four-backticks"),
    pytest.param("  ```toml\nkey = 1\n ```\n", "toml", 1, "key = 1\n", id="different-indents"),
    pytest.param(
        "1. item\n\n    ```yaml\n    key: value\n    ```\n",
        "yaml",
        3,
        "key: value\n",
        id="list-container",
    ),
    pytest.param("~~~toml\nkey = 1\n~~~\n", "toml", 1, "key = 1\n", id="tildes"),
    pytest.param(
        '```json linenums="1"\n{}\n```\n',
        "json",
        1,
        "{}\n",
        id="direct-attributes",
    ),
    pytest.param(
        "```{.yaml #sample}\nkey: value\n```\n",
        "yaml",
        1,
        "key: value\n",
        id="attribute-list",
    ),
]

KNOWN_INVALID_MARKDOWN = [
    pytest.param(
        '```toml\naddopts = "-n 4 ' + "\\" + '\n  --dist=worksteal"\n```\n',
        id="single-line-toml-continuation",
    ),
    pytest.param('```toml\n[tools]\n...\nuv = "latest"\n```\n', id="toml-ellipsis"),
    pytest.param(
        '```toml\ncargo-deny-version = "aqua:example"\ncargo-deny-version = "cargo-deny@latest"\n```\n',
        id="duplicate-toml-key",
    ),
]

MAIN_ERROR_CASES = [
    pytest.param("toml", "key =", id="toml"),
    pytest.param("yaml", "key: [", id="yaml"),
    pytest.param("json", "{", id="json"),
]


def init_git_repo(path: pathlib.Path) -> None:
    """テスト用の空Gitリポジトリを初期化する。"""
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)


def write_tracked(path: pathlib.Path, name: str, content: str) -> None:
    """ファイルを作成してGitの追跡対象へ追加する。"""
    (path / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "--", name], cwd=path, check=True)


def read_doc_codeblock_error_pattern() -> str:
    """実リポジトリのカスタム診断パターンを読み込む。"""
    source = tomlkit.parse((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pattern = source["tool"]["pyfltr"]["custom-commands"]["doc-codeblock-check"]["error-pattern"]
    assert isinstance(pattern, str)
    return pattern


def write_doc_codeblock_config(path: pathlib.Path) -> None:
    """実リポジトリと同じカスタム診断設定をテスト用リポジトリへ書き込む。"""
    document = tomlkit.document()
    document["tool"] = {
        "pyfltr": {
            "custom-commands": {
                "doc-codeblock-check": {
                    "type": "linter",
                    "path": sys.executable,
                    "args": [str(PROJECT_ROOT / "scripts/check_doc_codeblocks.py")],
                    "targets": "*.md",
                    "pass-filenames": False,
                    "error-pattern": read_doc_codeblock_error_pattern(),
                    "fast": True,
                }
            }
        }
    }
    (path / "pyproject.toml").write_text(tomlkit.dumps(document), encoding="utf-8")


@pytest.mark.parametrize(("markdown", "language", "lineno", "body"), FENCE_CASES)
def test_supported_fence_forms_are_extracted_and_parsed(
    markdown: str,
    language: str,
    lineno: int,
    body: str,
    tmp_path: pathlib.Path,
) -> None:
    blocks = list(check_doc_codeblocks.iter_code_blocks(markdown))
    assert blocks == [(language, lineno, body)]

    path = tmp_path / "sample.md"
    path.write_text(markdown, encoding="utf-8")
    assert not check_doc_codeblocks.check_file(path)


def test_four_space_indented_code_is_not_a_fence() -> None:
    markdown = "    ```yaml\nkey: value\n    ```\n"

    assert not list(check_doc_codeblocks.iter_code_blocks(markdown))


def test_unsupported_language_is_not_parsed(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "sample.md"
    path.write_text("```python\nnot valid Python\n```\n", encoding="utf-8")

    assert not check_doc_codeblocks.check_file(path)


@pytest.mark.parametrize(
    ("language", "body"),
    [("toml", "key ="), ("yaml", "key: ["), ("json", "{")],
)
def test_supported_parser_errors_are_reported(
    language: str,
    body: str,
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "sample.md"
    path.write_text(f"```{language}\n{body}\n```\n", encoding="utf-8")

    errors = check_doc_codeblocks.check_file(path)

    assert len(errors) == 1
    assert f"{language}コードブロックを解析できない" in errors[0]


def test_multiple_yaml_documents_are_parsed(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "sample.md"
    path.write_text(
        "```yaml\nfirst: 1\n---\nsecond: 2\n```\n",
        encoding="utf-8",
    )

    assert not check_doc_codeblocks.check_file(path)


def test_parser_error_in_later_yaml_document_is_reported(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "sample.md"
    path.write_text(
        "```yaml\nfirst: 1\n---\nsecond: [\n```\n",
        encoding="utf-8",
    )

    errors = check_doc_codeblocks.check_file(path)

    assert len(errors) == 1
    assert "yamlコードブロックを解析できない" in errors[0]


@pytest.mark.parametrize("markdown", KNOWN_INVALID_MARKDOWN)
def test_known_invalid_toml_examples_are_reported(
    markdown: str,
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "sample.md"
    path.write_text(markdown, encoding="utf-8")

    assert len(check_doc_codeblocks.check_file(path)) == 1


def test_unexpected_parser_error_is_raised(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    def raise_runtime_error(_text: str) -> object:
        raise RuntimeError("unexpected parser error")

    monkeypatch.setitem(check_doc_codeblocks.PARSERS, "toml", raise_runtime_error)
    path = tmp_path / "sample.md"
    path.write_text("```toml\nkey = 1\n```\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unexpected parser error"):
        check_doc_codeblocks.check_file(path)


def test_main_returns_zero_and_ignores_untracked_markdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_git_repo(tmp_path)
    write_tracked(tmp_path, "valid.md", "```toml\nkey = 1\n```\n")
    (tmp_path / "untracked.md").write_text("```toml\nkey =\n```\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert check_doc_codeblocks.main() == 0
    assert capsys.readouterr().out == ""


def test_main_ignores_deleted_tracked_markdown_on_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_git_repo(tmp_path)
    write_tracked(tmp_path, "deleted.md", "```toml\nkey = 1\n```\n")
    monkeypatch.chdir(tmp_path)

    assert check_doc_codeblocks.main() == 0
    assert capsys.readouterr().out == ""

    (tmp_path / "deleted.md").unlink()

    assert check_doc_codeblocks.main() == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("filename", "diagnostic_path"),
    [
        pytest.param("line\nbreak.md", "line%0Abreak.md", id="newline"),
        pytest.param("page\fbreak.md", "page%0Cbreak.md", id="form-feed"),
        pytest.param("colon:name.md", "colon:name.md", id="colon"),
        pytest.param("percent%0A.md", "percent%250A.md", id="percent"),
        pytest.param("docs/sample.md", "docs/sample.md", id="path-separator"),
    ],
)
def test_check_file_formats_diagnostic_path_without_file_io(
    filename: str,
    diagnostic_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def read_text(_path: pathlib.Path, **_kwargs: object) -> str:
        return "```toml\nkey =\n```\n"

    monkeypatch.setattr(pathlib.Path, "read_text", read_text)

    errors = check_doc_codeblocks.check_file(pathlib.Path(filename))

    assert len(errors) == 1
    assert errors[0].startswith(f"{diagnostic_path}:1:1: tomlコードブロックを解析できない: ")


def test_error_pattern_parses_special_character_paths() -> None:
    output = "\n".join(
        [
            "line%0Abreak.md:1:1: newline path",
            "colon:name.md:2:3: colon path",
        ]
    )

    errors = pyfltr.command.error_parser.parse_errors(
        "doc-codeblock-check",
        output,
        error_pattern=read_doc_codeblock_error_pattern(),
    )

    assert [(error.file, error.line, error.col) for error in errors] == [
        ("line%0Abreak.md", 1, 1),
        ("colon:name.md", 2, 3),
    ]


def test_pyfltr_custom_command_reports_portable_tracked_paths(tmp_path: pathlib.Path) -> None:
    init_git_repo(tmp_path)
    filenames = ("invalid-one.md", "invalid-two.md")
    for filename in filenames:
        write_tracked(tmp_path, filename, "```toml\nkey =\n```\n")
    write_doc_codeblock_config(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyfltr.cli.main",
            "run",
            "--no-cache",
            "--no-ui",
            "--quiet",
            "--output-format=jsonl",
            "--commands=doc-codeblock-check",
            "--work-dir",
            str(tmp_path),
            str(tmp_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    records = [json.loads(line) for line in result.stdout.splitlines()]
    diagnostics = [record for record in records if record["kind"] == "diagnostic"]
    assert {record["file"] for record in diagnostics} == set(filenames)
    assert all(message["line"] == 1 and message["col"] == 1 for record in diagnostics for message in record["messages"])


@pytest.mark.parametrize(("language", "body"), MAIN_ERROR_CASES)
def test_main_reports_one_line_tracked_error_and_returns_one(
    language: str,
    body: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_git_repo(tmp_path)
    filename = f"invalid-{language}.md"
    write_tracked(tmp_path, filename, f"intro\n```{language}\n{body}\n```\n")
    monkeypatch.chdir(tmp_path)

    assert check_doc_codeblocks.main() == 1
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    match = ERROR_PATTERN.fullmatch(lines[0])
    assert match is not None
    assert match.group("file") == filename
    assert match.group("line") == "2"
    assert match.group("col") == "1"
    assert match.group("message").startswith(f"{language}コードブロックを解析できない: ")
