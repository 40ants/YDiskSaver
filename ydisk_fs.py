"""Safe local writes for a one-way Yandex Disk copy."""

import errno
import os
from pathlib import Path
import stat
import tempfile
from threading import Lock
import time
from typing import Optional
import unicodedata


_PUBLICATION_LOCKS = tuple(Lock() for _ in range(64))


def _publish(temporary: Path, destination: Path) -> bool:
    """Publish a complete temporary file once, including on FAT filesystems."""
    key = unicodedata.normalize("NFC", str(destination)).casefold()
    with _PUBLICATION_LOCKS[hash(key) % len(_PUBLICATION_LOCKS)]:
        if destination.exists() or destination.is_symlink():
            return False
        try:
            os.link(temporary, destination)
        except FileExistsError:
            return False
        except OSError as error:
            if error.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS):
                raise
            # The temporary file is complete and in the destination directory.
            # The lock protects the existence check and replace from other workers.
            if destination.exists() or destination.is_symlink():
                return False
            os.replace(temporary, destination)
        return True


class IncompleteFileError(IOError):
    def __init__(self, actual_size: int, expected_size: int, saved_path: Optional[Path]):
        super().__init__(f"Получен неполный файл: {actual_size} из {expected_size} байт")
        self.saved_path = saved_path


def safe_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or name in ("", ".", "..")
        or "/" in name
        or "\\" in name
        or "\0" in name
    ):
        raise ValueError("Некорректное имя файла в списке Диска")
    return name


def prepare_root(path: Path) -> Path:
    path = path.expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve(strict=True)


def ensure_directory(path: Path, root: Path) -> None:
    path = Path(os.path.abspath(path))
    if path != root and root not in path.parents:
        raise ValueError("Путь вышел за пределы папки назначения")
    if path != root:
        ensure_directory(path.parent, root)
    try:
        status = path.lstat()
    except FileNotFoundError:
        path.mkdir()
        return
    if not stat.S_ISDIR(status.st_mode):
        raise ValueError(f"Небезопасная папка назначения: {path}")


def save_stream(source, destination: Path, root: Path, expected_size=None, deadline=None,
                save_incomplete=False, incomplete_name=None) -> str:
    """Copy a binary stream and publish it only if the target does not exist."""
    destination = Path(os.path.abspath(destination))
    if root not in destination.parents:
        raise ValueError("Путь вышел за пределы папки назначения")
    ensure_directory(destination.parent, root)
    if destination.exists() or destination.is_symlink():
        return "skipped"

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".yadisk-copy-", suffix=".part",
            dir=destination.parent, delete=False,
        ) as output:
            temporary = Path(output.name)
            size = 0
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("Превышено время скачивания файла")
                chunk = source.read(256 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                size += len(chunk)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Превышено время скачивания файла")
        if expected_size is not None and size != expected_size:
            saved_path = None
            if save_incomplete:
                incomplete = destination.with_name(
                    incomplete_name or f"incomplete_{os.getpid()}-{destination.name}"
                )
                if _publish(temporary, incomplete):
                    saved_path = incomplete
            raise IncompleteFileError(size, expected_size, saved_path)
        return "downloaded" if _publish(temporary, destination) else "skipped"
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
