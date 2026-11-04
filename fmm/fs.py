"""Safe file operations: removal without following links, atomic writes, free space."""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Optional


class UnsafeRemoval(Exception):
    pass


def _check_same_device(root: Path) -> None:
    """Refuses to remove a tree that contains a mount point (another volume)."""
    device = os.lstat(root).st_dev
    pending = [str(root)]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except PermissionError as exc:
            raise UnsafeRemoval('no permission to read %s' % current) from exc
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                if entry.stat(follow_symlinks=False).st_dev != device:
                    raise UnsafeRemoval('contains another mounted volume: %s' % entry.path)
                pending.append(entry.path)


def safe_remove(path) -> bool:
    """Removes a file, link or folder without following links. Returns False if it did not exist."""
    p = Path(path)
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        os.unlink(p)
        return True
    if os.path.ismount(p):
        raise UnsafeRemoval('%s is a mount point' % p)
    if not shutil.rmtree.avoids_symlink_attacks:
        raise UnsafeRemoval('this Python cannot remove folders in a way that resists symlink attacks')
    _check_same_device(p)
    shutil.rmtree(p)
    return True


def write_private(path, text: str) -> None:
    """Creates a new file with mode 600; never overwrites."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def write_atomic(path, text) -> None:
    """Replaces the content (text or bytes) keeping the mode; writes to a temporary file in the same folder."""
    p = Path(path)
    mode = stat.S_IMODE(os.stat(p).st_mode) if p.exists() else 0o644
    fd, temporary = tempfile.mkstemp(prefix='.' + p.name + '.', dir=str(p.parent))
    try:
        with os.fdopen(fd, 'wb' if isinstance(text, bytes) else 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, p)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def copy_preserving(source, destination) -> Optional[Path]:
    """Copies a file or folder to the backup; skips what does not exist."""
    src, dst = Path(source), Path(destination)
    if not os.path.lexists(src):
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)
    return dst


def disk_free(path) -> int:
    target = Path('/System/Volumes/Data')
    if not target.exists():
        target = Path(path)
    return shutil.disk_usage(target).free


def disk_total(path) -> int:
    target = Path('/System/Volumes/Data')
    if not target.exists():
        target = Path(path)
    return shutil.disk_usage(target).total
