"""Host-side file operations. Never source/evaluate .env as shell code."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def set_image(image: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9./_:@-]+", image):
        raise ValueError("invalid image reference")
    path = Path(".env")
    content = path.read_text(encoding="utf-8")
    content, count = re.subn(r"(?m)^COLLECTOR_IMAGE=.*$", f"COLLECTOR_IMAGE={image}", content)
    if count != 1:
        raise ValueError(".env must have exactly one COLLECTOR_IMAGE")
    temporary = path.with_suffix(".env.partial")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def usb_env(destination: str, collector_image: str, postgres_image: str) -> None:
    content = Path(".env.example").read_text(encoding="utf-8")
    for key, image in (("COLLECTOR_IMAGE", collector_image), ("POSTGRES_IMAGE", postgres_image)):
        if not re.fullmatch(r"collector-offline-usb/(collector|postgres):sha-[a-f0-9]{64}", image):
            raise ValueError("invalid USB image alias")
        content = re.sub(rf"(?m)^{key}=.*$", f"{key}={image}", content)
    Path(destination).write_text(content, encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "set-image":
        set_image(sys.argv[2])
    elif len(sys.argv) == 5 and sys.argv[1] == "usb-env":
        usb_env(*sys.argv[2:])
    else:
        raise SystemExit("usage: ops.py set-image IMAGE | usb-env DEST COLLECTOR POSTGRES")
