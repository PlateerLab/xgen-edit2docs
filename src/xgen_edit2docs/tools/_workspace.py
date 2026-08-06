"""Ephemeral temp workspaces for tool functions that wrap disk-based scripts.

The core engine inherited from ppt-master is mostly path-based (it reads files
and writes files). The tool layer hides this from callers by spinning up an
isolated temp directory per call, populating it with the inputs, running the
disk-based function, capturing the outputs, and tearing the directory down.

Workspaces are deleted on context exit — never leak across requests / tenants.

See ppt-master-analysis/04-integration-plan.md §F1 (filesystem-assumption removal).
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def temp_workspace(prefix: str = "xgen_edit2docs-") -> Iterator[Path]:
    """Yield a fresh temp dir; remove it on exit (even on exception)."""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def write_bytes(workspace: Path, name: str, content: bytes) -> Path:
    """Write *content* into *workspace*/<name>, creating parent dirs as needed."""
    target = workspace / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def write_text(workspace: Path, name: str, content: str, encoding: str = "utf-8") -> Path:
    target = workspace / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding=encoding)
    return target
