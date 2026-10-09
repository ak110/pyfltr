"""採取対象ツールごとの小さなサンプル定義。

各サンプルは診断を発生させる最小の入力と、その入力をpyfltrで実行するための`[tool.pyfltr]`設定を持つ。
採取対象の一覧は`pyfltr.tools.BUILTIN_COMMANDS`を典拠とし、本モジュールに定義の無いツールは
採取前に検出する（`missing_samples`）。
"""

import dataclasses
import json
import typing

import pyfltr.tools


@dataclasses.dataclass(frozen=True)
class Sample:
    """1回のpyfltr実行で採取する入力。"""

    id: str
    """出力ディレクトリ名と比較入力の識別子に使う一意な名前。"""
    tool: str
    """`--commands`へ渡すpyfltrのコマンド名。"""
    files: dict[str, str]
    """サンプルディレクトリ相対のパスと内容。"""
    targets: tuple[str, ...]
    """pyfltrへ位置引数で渡すパス。"""
    settings: dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    """`[tool.pyfltr]`へ書く追加設定（`<tool> = true`と`respect-gitignore = false`は常に書く）。"""
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    """pyfltrの起動時に追加する環境変数。"""
    git: bool = False
    """サンプルディレクトリを`git init`し、全ファイルをcommitするか。"""
    setup: tuple[tuple[str, ...], ...] = ()
    """pyfltrの前に実行する準備コマンド（ロックファイルの生成など）。対応ツール自体は起動しない。"""
    description: str = ""


def _toml_value(value: typing.Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"TOMLへ変換できない設定値: {value!r}")


def pyfltr_settings(sample: Sample) -> dict[str, typing.Any]:
    """サンプルの`[tool.pyfltr]`へ書く設定全体を返す。"""
    return {sample.tool: True, "respect-gitignore": False, **sample.settings}


def render_files(sample: Sample) -> dict[str, str]:
    """`pyproject.toml`へ`[tool.pyfltr]`を加えたサンプルのファイル一式を返す。"""
    section = "[tool.pyfltr]\n" + "".join(f"{key} = {_toml_value(value)}\n" for key, value in pyfltr_settings(sample).items())
    files = dict(sample.files)
    existing = files.get("pyproject.toml")
    files["pyproject.toml"] = (existing.rstrip("\n") + "\n\n" if existing else "") + section
    return files


def _sample(tool: str, files: dict[str, str], targets: tuple[str, ...], **kwargs: typing.Any) -> Sample:
    return Sample(id=tool, tool=tool, files=files, targets=targets, **kwargs)


_PACKAGE_JSON = '{\n  "name": "parser-review-sample",\n  "private": true,\n  "type": "module"\n}\n'
_TSCONFIG = (
    '{\n  "compilerOptions": {\n    "target": "ES2022",\n    "module": "ES2022",\n'
    '    "moduleResolution": "Bundler",\n    "strict": true,\n    "noEmit": true,\n    "types": []\n  },\n'
    '  "include": ["sample.ts"]\n}\n'
)
_PRE_COMMIT_CONFIG = (
    "repos:\n  - repo: local\n    hooks:\n      - id: sample-fail\n        name: sample-fail\n"
    "        entry: sample failure message\n        language: fail\n        files: \\.py$\n"
)
_CARGO_TOML = '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\nlicense = "MIT"\npublish = false\n'
_CSPROJ = (
    '<Project Sdk="Microsoft.NET.Sdk">\n  <PropertyGroup>\n    <OutputType>Exe</OutputType>\n'
    "    <TargetFramework>net10.0</TargetFramework>\n  </PropertyGroup>\n</Project>\n"
)
_TEST_CSPROJ = (
    '<Project Sdk="Microsoft.NET.Sdk">\n  <PropertyGroup>\n    <TargetFramework>net10.0</TargetFramework>\n'
    "    <IsPackable>false</IsPackable>\n  </PropertyGroup>\n  <ItemGroup>\n"
    '    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.*" />\n'
    '    <PackageReference Include="xunit" Version="2.*" />\n'
    '    <PackageReference Include="xunit.runner.visualstudio" Version="2.*" />\n'
    "  </ItemGroup>\n</Project>\n"
)
_VULNERABLE_PACKAGE_JSON = (
    '{\n  "name": "parser-review-sample",\n  "private": true,\n  "dependencies": {"lodash": "4.17.0"}\n}\n'
)
_DUPLICATED_BODY = "".join(f"    total = total + {index} * value\n" for index in range(18))


def _github_token_like() -> str:
    # 固定の検出対象文字列をリポジトリへ置かないよう、実行時に組み立てる。
    body = "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0hJ3lM6xR"
    return "gh" + "p_" + body[:36]


SAMPLES: tuple[Sample, ...] = (
    _sample("prettier", {"package.json": _PACKAGE_JSON, "sample.ts": "const value={a:1,b:2}\n"}, ("sample.ts",)),
    _sample("ruff-format", {"sample.py": "value=1\n"}, ("sample.py",)),
    _sample(
        "uv-sort",
        {
            "pyproject.toml": '[project]\nname = "sample"\nversion = "0.0.0"\nrequires-python = ">=3.11"\n'
            'dependencies = ["requests>=2.31", "click>=8.0"]\n'
        },
        ("pyproject.toml",),
    ),
    _sample("shfmt", {"sample.sh": "#!/bin/sh\nif true;then\necho sample\nfi\n"}, ("sample.sh",)),
    _sample("taplo", {"sample.toml": "key=1\n"}, ("sample.toml",)),
    _sample(
        "cargo-fmt", {"Cargo.toml": _CARGO_TOML, "src/main.rs": 'fn main(){let x=1;println!("{}",x);}\n'}, ("src/main.rs",)
    ),
    _sample(
        "dotnet-format",
        {
            "sample.csproj": _CSPROJ,
            "Program.cs": "class Program{static void Main(){int  value=1;System.Console.WriteLine(value);}}\n",
        },
        ("Program.cs",),
    ),
    _sample("prek", {".pre-commit-config.yaml": _PRE_COMMIT_CONFIG, "sample.py": "VALUE = 1\n"}, ("sample.py",), git=True),
    _sample(
        "pre-commit", {".pre-commit-config.yaml": _PRE_COMMIT_CONFIG, "sample.py": "VALUE = 1\n"}, ("sample.py",), git=True
    ),
    _sample(
        "ec",
        {
            ".editorconfig": "root = true\n\n[*]\ntrim_trailing_whitespace = true\ninsert_final_newline = true\n",
            "sample.txt": "trailing   \n",
        },
        ("sample.txt",),
    ),
    _sample("shellcheck", {"sample.sh": "#!/bin/sh\necho $1\n"}, ("sample.sh",)),
    # 検出対象の綴り誤り・口語表現はリポジトリ自身の検査に掛からないよう、語を分けて組み立てる。
    _sample("typos", {"sample.txt": "t" + "eh value is rec" + "ieved\n"}, ("sample.txt",)),
    _sample(
        "actionlint",
        {
            ".github/workflows/ci.yaml": "name: sample\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo ${{ unknown.value }}\n"
        },
        (".github/workflows/ci.yaml",),
    ),
    _sample(
        "pinact",
        {
            ".github/workflows/ci.yaml": "name: sample\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - uses: actions/checkout@v4\n"
        },
        (".github/workflows/ci.yaml",),
    ),
    _sample("glab-ci-lint", {".gitlab-ci.yml": "job:\n  script: 1\n  unknown_key: true\n"}, (".gitlab-ci.yml",), git=True),
    _sample("yamllint", {"sample.yaml": "key: value   \nother:  1\n"}, ("sample.yaml",)),
    _sample("hadolint", {"Dockerfile": "FROM debian\nRUN apt-get update && apt-get install curl\n"}, ("Dockerfile",)),
    # gitleaksは`detect`でgit履歴を走査するため、サンプルをcommitする。
    _sample("gitleaks", {"sample.txt": ""}, ("sample.txt",), git=True),
    _sample(
        "semgrep",
        {
            ".semgrep.yml": "rules:\n  - id: sample-eval\n    pattern: eval(...)\n    message: avoid eval\n"
            "    languages: [python]\n    severity: ERROR\n",
            "sample.py": 'eval("1")\n',
        },
        ("sample.py",),
        settings={"semgrep-args": ["scan", "--json", "--error", "--metrics=off", "--config", ".semgrep.yml"]},
    ),
    _sample("bandit", {"sample.py": 'import subprocess\n\nsubprocess.call("ls", shell=True)\n'}, ("sample.py",)),
    _sample("pylint", {"sample.py": '"""sample."""\n\nimport os\n'}, ("sample.py",)),
    _sample("mypy", {"sample.py": 'value: int = "wrong"\n'}, ("sample.py",)),
    _sample("ruff-check", {"sample.py": "import os\n"}, ("sample.py",)),
    _sample("pyright", {"sample.py": 'value: int = "wrong"\n'}, ("sample.py",)),
    _sample("ty", {"sample.py": 'value: int = "wrong"\n'}, ("sample.py",)),
    _sample(
        "arid",
        {
            "sample.py": "def first(value):\n    total = 0\n" + _DUPLICATED_BODY + "    return total\n\n\n"
            "def second(value):\n    total = 0\n" + _DUPLICATED_BODY + "    return total\n"
        },
        ("sample.py",),
    ),
    _sample("markdownlint", {"package.json": _PACKAGE_JSON, "sample.md": "#Heading\n\ntext  \n"}, ("sample.md",)),
    _sample(
        "textlint",
        {
            "package.json": '{\n  "name": "parser-review-sample",\n  "private": true,\n'
            '  "textlint": {"rules": {"preset-ja-technical-writing": true}}\n}\n',
            "sample.md": "# 見出し\n\nこれは、とても、長い、文、で、読点、が、多すぎる、ので、エラー、に、なる。\n",
        },
        ("sample.md",),
    ),
    _sample(
        "designmd",
        {
            "package.json": _PACKAGE_JSON,
            "DESIGN.md": '---\nname: sample\ncolors:\n  primary: "{colors.missing}"\n---\n\n# Design\n',
        },
        ("DESIGN.md",),
    ),
    _sample("lychee", {"sample.md": "# sample\n\n[missing](./missing-file.md)\n"}, ("sample.md",)),
    _sample("colloquial-check", {"sample.md": "# sample\n\nこれで足" + "りる。\n"}, ("sample.md",)),
    _sample(
        "tsc",
        {"package.json": _PACKAGE_JSON, "tsconfig.json": _TSCONFIG, "sample.ts": 'export const value: number = "wrong";\n'},
        ("sample.ts",),
    ),
    _sample(
        "eslint",
        {
            "package.json": _PACKAGE_JSON,
            "eslint.config.js": 'export default [{ rules: { "no-unused-vars": "error", "no-debugger": "error" } }];\n',
            "sample.js": "const unused = 1;\ndebugger;\n",
        },
        ("sample.js",),
    ),
    _sample("biome", {"package.json": _PACKAGE_JSON, "sample.ts": "var value = 1;\ndebugger;\n"}, ("sample.ts",)),
    _sample("oxlint", {"package.json": _PACKAGE_JSON, "sample.js": "const value = 1;\nvalue = 2;\n"}, ("sample.js",)),
    _sample(
        "cargo-clippy",
        {"Cargo.toml": _CARGO_TOML, "src/main.rs": "fn main() {\n    let value = 1;\n    let _ = value == value;\n}\n"},
        ("src/main.rs",),
    ),
    _sample(
        "cargo-check",
        {"Cargo.toml": _CARGO_TOML, "src/main.rs": 'fn main() {\n    let value: i32 = "wrong";\n    println!("{value}");\n}\n'},
        ("src/main.rs",),
    ),
    _sample(
        "cargo-deny",
        {"Cargo.toml": _CARGO_TOML, "src/main.rs": "fn main() {}\n", "deny.toml": "[licenses]\nallow = []\n"},
        ("Cargo.toml",),
    ),
    _sample(
        "dotnet-build",
        {"sample.csproj": _CSPROJ, "Program.cs": 'int value = "wrong";\nSystem.Console.WriteLine(value);\n'},
        ("Program.cs",),
    ),
    _sample(
        "sqlfluff",
        {".sqlfluff": "[sqlfluff]\ndialect = ansi\n", "sample.sql": "SELECT a from b\n"},
        ("sample.sql",),
    ),
    _sample(
        "uv-audit",
        {
            "pyproject.toml": '[project]\nname = "sample"\nversion = "0.0.0"\nrequires-python = ">=3.11"\n'
            'dependencies = ["jinja2==2.10"]\n'
        },
        ("pyproject.toml",),
        setup=(("uv", "lock", "--no-progress"),),
    ),
    _sample(
        "pnpm-audit",
        {"package.json": _VULNERABLE_PACKAGE_JSON},
        ("package.json",),
        setup=(("pnpm", "install", "--lockfile-only", "--ignore-scripts"),),
    ),
    _sample(
        "npm-audit",
        {"package.json": _VULNERABLE_PACKAGE_JSON},
        ("package.json",),
        setup=(("npm", "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund"),),
    ),
    _sample(
        "yarn-audit",
        {"package.json": _VULNERABLE_PACKAGE_JSON},
        ("package.json",),
        setup=(("yarn", "install", "--ignore-scripts", "--non-interactive", "--no-progress"),),
    ),
    _sample(
        "pytest",
        {
            "pyproject.toml": '[tool.pytest.ini_options]\nasyncio_default_fixture_loop_scope = "function"\n',
            "sample_test.py": "def test_sample():\n    assert 1 == 2\n",
        },
        ("sample_test.py",),
    ),
    _sample(
        "vitest",
        {"package.json": _PACKAGE_JSON, "sample.test.ts": 'throw new Error("sample failure");\n'},
        ("sample.test.ts",),
    ),
    _sample(
        "cargo-test",
        {
            "Cargo.toml": _CARGO_TOML,
            "src/lib.rs": "#[cfg(test)]\nmod tests {\n    #[test]\n    fn sample() {\n        assert_eq!(1, 2);\n    }\n}\n",
        },
        ("src/lib.rs",),
    ),
    _sample(
        "dotnet-test",
        {
            "sample.csproj": _TEST_CSPROJ,
            "SampleTest.cs": (
                "public class SampleTest\n{\n    [Xunit.Fact]\n    public void Fails() => Xunit.Assert.Equal(1, 2);\n}\n"
            ),
        },
        ("SampleTest.cs",),
    ),
)


def _with_runtime_content(sample: Sample) -> Sample:
    if sample.tool == "gitleaks":
        return dataclasses.replace(sample, files={"sample.txt": f"token = {_github_token_like()}\n"})
    return sample


def builtin_samples() -> dict[str, Sample]:
    """ツール名からサンプルへの対応を返す。"""
    return {sample.tool: _with_runtime_content(sample) for sample in SAMPLES}


def missing_samples() -> list[str]:
    """`BUILTIN_COMMANDS`のうちサンプル定義の無いツール名を返す。"""
    defined = {sample.tool for sample in SAMPLES}
    return [name for name in pyfltr.tools.BUILTIN_COMMANDS if name not in defined]


def has_parse_definition(tool: str) -> bool:
    """`parse_errors`が診断を取り出す解析定義をツールが持つか。"""
    info = pyfltr.tools.BUILTIN_COMMANDS.get(tool)
    if info is None:
        return False
    return bool(info.diagnostic_pattern or info.parser or info.path_resolving_parser)


def select_samples(tools: list[str] | None) -> list[Sample]:
    """指定ツール（Noneなら全`BUILTIN_COMMANDS`）のサンプルを`BUILTIN_COMMANDS`の順で返す。

    未知のツール名とサンプル定義の欠落は採取前に`ValueError`とする。
    """
    missing = missing_samples()
    if missing:
        raise ValueError(f"サンプル定義の無いツールがあります: {', '.join(missing)}")
    samples = builtin_samples()
    names = list(pyfltr.tools.BUILTIN_COMMANDS)
    if tools is None:
        return [samples[name] for name in names]
    unknown = [tool for tool in tools if tool not in samples]
    if unknown:
        raise ValueError(f"未知のツール名です: {', '.join(unknown)}")
    return [samples[name] for name in names if name in tools]
