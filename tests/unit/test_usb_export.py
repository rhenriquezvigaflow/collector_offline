from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


def test_usb_distribution_excludes_plant_files_and_uses_loadable_aliases(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    if os.name == "nt":
        git_bash = Path("C:/Program Files/Git/bin/bash.exe")
        bash = str(git_bash) if git_bash.exists() else None
    if not bash:
        pytest.skip("Bash is required to exercise the release packager")
    source = Path(__file__).parents[2]
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for path in source.glob("*.sh"):
        shutil.copy2(path, checkout / path.name)
    for name in (
        "compose.yml",
        ".env.example",
        "site.example.yml",
        ".gitattributes",
        "ARQUITECTURA_DOCKER.md",
        "INSTALACION_MINIPC.md",
        "OPERACION_MANTENIMIENTO.md",
    ):
        shutil.copy2(source / name, checkout / name)
    for folder in ("docker", "docs"):
        shutil.copytree(
            source / folder, checkout / folder, ignore=shutil.ignore_patterns("__pycache__")
        )
    shutil.copy2(checkout / ".env.example", checkout / ".env")
    (checkout / "runtime/config").mkdir(parents=True)
    (checkout / "runtime/config/site.yml").write_text("PRIVATE_PLANT_SENTINEL", encoding="utf-8")
    (checkout / "rhenriquez").write_text("PRIVATE_KEY_SENTINEL", encoding="utf-8")
    tools = tmp_path / "bin"
    tools.mkdir()
    fake = tools / "docker"
    fake.write_text(
        """#!/usr/bin/env bash
set -eu
if [[ "$1" == info || "$1" == tag ]]; then exit 0; fi
if [[ "$1" == save ]]; then printf test-image-archive > "$3"; exit 0; fi
if [[ "$1" == image ]]; then
  if [[ "$*" == *Architecture* ]]; then echo amd64;
  elif [[ "$*" == *Id* ]]; then printf 'sha256:%064d\\n' 1; fi
  exit 0
fi
if [[ "$1" == compose ]]; then
  if [[ "$*" == *--images* ]]; then printf 'collector:test\\npostgres:test\\n';
  elif [[ "$*" == *--format* ]]; then
    printf '%s%s\\n' '{"services":{"collector":{"image":"collector:test"},' \\
      '"postgres":{"image":"postgres:test"}}}'
  fi
  exit 0
fi
exit 1
""",
        encoding="utf-8",
        newline="\n",
    )
    fake.chmod(0o755)
    python = tools / "python3"
    python.write_text(
        f'#!/usr/bin/env bash\nexec "{Path(sys.executable).as_posix()}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    python.chmod(0o755)
    bundle = tmp_path / "bundle"
    env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run(
        [bash, "export-usb.sh", str(bundle)],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with tarfile.open(bundle / "collector-distribution.tar.gz") as package:
        names = package.getnames()
        assert ".env.usb" in names
        assert not any(name.startswith(("runtime/", "config/", ".git/")) for name in names)
        assert "rhenriquez" not in names and ".env" not in names
        for item in package.getmembers():
            if item.isfile():
                contents = package.extractfile(item).read()
                assert b"PRIVATE_PLANT_SENTINEL" not in contents
                assert b"PRIVATE_KEY_SENTINEL" not in contents
    offline_env = (bundle / ".env.usb").read_text(encoding="utf-8")
    assert "COLLECTOR_IMAGE=collector-offline-usb/collector:sha-" in offline_env
    assert "POSTGRES_IMAGE=collector-offline-usb/postgres:sha-" in offline_env
    assert len((bundle / "images.txt").read_text().splitlines()) == 2
    assert len((bundle / "SHA256SUMS").read_text().splitlines()) == 4
