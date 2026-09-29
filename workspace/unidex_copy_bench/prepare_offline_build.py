#!/usr/bin/env python3
"""Apply and record build-entry adaptations after upstream hash verification."""

import argparse
import difflib
import hashlib
import json
from pathlib import Path
import shlex
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("log_dir", type=Path)
    args = parser.parse_args()
    interpreter = sys.executable
    if any(char in interpreter for char in ('"', ';', '\n', '\r')):
        raise RuntimeError("unsupported interpreter path for CMake")
    python_command = shlex.quote(interpreter)
    commit = (args.source / "unidex-source-manifest/source_commit.txt").read_text().strip()
    if commit != "d9261669b0303a28369d07c0eea0bd1627235dd6":
        raise RuntimeError("unexpected source commit")
    original = {name: (args.source / name).read_text() for name in (
        "build.sh", "cmake/config_envs.cmake", "cmake/config_git_last_commit.cmake",
    )}
    build = original["build.sh"]
    for expected in ("python3 setup.py", "pip3 show wheel", "pip3 install wheel==0.45.1"):
        if expected not in build:
            raise RuntimeError(f"upstream build entry changed: {expected}")
    build = build.replace("python3 setup.py", f"{python_command} setup.py")
    build = build.replace("pip3 show wheel", f"{python_command} -m pip show wheel")
    build = build.replace("pip3 install wheel==0.45.1", f"{python_command} -m pip install wheel==0.45.1")
    envs = original["cmake/config_envs.cmake"]
    expected = "find_program(PYTHON_EXECUTABLE NAMES python3)"
    if envs.count(expected) != 1:
        raise RuntimeError("upstream CMake interpreter entry changed")
    envs = envs.replace(expected, f'set(PYTHON_EXECUTABLE "{interpreter}" CACHE FILEPATH "Pinned installer Python" FORCE)')
    if "COMMAND ${GIT_EXECUTABLE} rev-parse HEAD" not in original["cmake/config_git_last_commit.cmake"]:
        raise RuntimeError("upstream CMake commit entry changed")
    git_config = (
        'file(READ "${PROJECT_SOURCE_DIR}/unidex-source-manifest/source_commit.txt" GIT_COMMIT_ID)\n'
        'string(STRIP "${GIT_COMMIT_ID}" GIT_COMMIT_ID)\n'
        'add_compile_definitions(GIT_LAST_COMMIT=${GIT_COMMIT_ID})\n'
        'message(STATUS "Offline manifest commit: ${GIT_COMMIT_ID}")\n'
    )
    adapted = {"build.sh": build, "cmake/config_envs.cmake": envs,
               "cmake/config_git_last_commit.cmake": git_config}
    changes = []
    diff = []
    for name, content in adapted.items():
        backup = args.log_dir / "upstream-build-entry" / name
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("x") as stream:
            stream.write(original[name])
        changes.append(dict(file=name, before_sha256=hashlib.sha256(original[name].encode()).hexdigest(),
                            after_sha256=hashlib.sha256(content.encode()).hexdigest()))
        diff.extend(difflib.unified_diff(original[name].splitlines(True), content.splitlines(True),
                                         fromfile=f"upstream/{name}", tofile=f"offline/{name}"))
        (args.source / name).write_text(content)
    with (args.log_dir / "build-entry.diff").open("x") as stream:
        stream.writelines(diff)
    with (args.log_dir / "build-adaptation.json").open("x") as stream:
        json.dump(dict(source_commit=commit, python_executable=interpreter, changes=changes,
                       native_operator_sources_modified=False), stream, indent=2)
        stream.write("\n")
    print(f"BUILD_ENTRY_READY python={interpreter}; adaptations recorded in build-entry.diff")


if __name__ == "__main__":
    raise SystemExit(main())
