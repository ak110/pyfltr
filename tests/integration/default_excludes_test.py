"""既定除外をCLIからformatterへの対象引渡しまで通して検証する。"""

import json
import pathlib
import sys

import tomlkit

import pyfltr.cli.main


def test_cli_default_exclude_keeps_generated_lock_files(tmp_path: pathlib.Path) -> None:
    """個別の除外設定なしで、生成ロックファイルをformatterへ渡さず書き換えない。

    miseの`.mise/locks/`配下の`aube-lock.yaml`は`mise.lock`がハッシュ値を記録するため、
    整形で書き換わると記録と一致しなくなる。`.mise`配下の利用者設定は対象に残る。
    """
    project = tmp_path / "project"
    lock_dir = project / ".mise" / "locks" / "npm-pnpm" / "12.17.0"
    lock_dir.mkdir(parents=True)
    aube_lock = lock_dir / "aube-lock.yaml"
    shrinkwrap = project / "npm-shrinkwrap.json"
    mise_config = project / ".mise" / "config.toml"
    plain = project / "config.yaml"
    originals = {
        aube_lock: "lockfileVersion: 1\n",
        shrinkwrap: "{}\n",
        mise_config: "[tools]\n",
        plain: "a: 1\n",
    }
    for path, text in originals.items():
        path.write_text(text, encoding="utf-8")

    # 受け取った対象を記録し、各ファイルへ追記して書き換えるformatter。
    output = tmp_path / "received.json"
    script = tmp_path / "probe.py"
    script.write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding='utf-8')\n"
        "for name in sys.argv[2:]:\n"
        "    path = pathlib.Path(name)\n"
        "    path.write_text(path.read_text(encoding='utf-8') + '# formatted\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    settings = {
        "respect-gitignore": False,
        "custom-commands": {
            "probe": {"type": "formatter", "path": sys.executable, "args": [str(script), str(output)], "targets": "*"}
        },
    }
    (project / "pyproject.toml").write_text(tomlkit.dumps({"tool": {"pyfltr": settings}}), encoding="utf-8")

    assert (
        pyfltr.cli.main.run(["run", "--work-dir", str(project), "--commands=probe", "--no-cache", "--no-archive", str(project)])
        == 0
    )

    # formatterはプロジェクトを作業ディレクトリとして起動され、相対パスを受け取る。
    received = {(project / name).resolve() for name in json.loads(output.read_text(encoding="utf-8"))}
    assert aube_lock.resolve() not in received
    assert shrinkwrap.resolve() not in received
    assert mise_config.resolve() in received
    assert plain.resolve() in received
    assert aube_lock.read_text(encoding="utf-8") == originals[aube_lock]
    assert shrinkwrap.read_text(encoding="utf-8") == originals[shrinkwrap]
