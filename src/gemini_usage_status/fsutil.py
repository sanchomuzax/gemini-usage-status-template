"""Small file helpers shared by the collector and the publisher."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


def atomic_write_text(path, text):
    """Write via a temp file and os.replace, so a reader never sees half a file."""
    target = Path(path)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()


@contextmanager
def exclusive_lock(path):
    """Block until this process holds an exclusive flock on `path`.

    The cron run and an agent's budget check may read the quota at the same
    moment; without this, both could start `agy models` and race on the token
    file. Falls through without a lock if the file cannot be opened.
    """
    try:
        handle = open(path, "a", encoding="utf-8")  # noqa: SIM115 -- held for the block
    except OSError:
        yield
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        handle.close()


def display_path(path):
    """A path for messages that may be published: the home directory as ~."""
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if text.startswith(home) else text
