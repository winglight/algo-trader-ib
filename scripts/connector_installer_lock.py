"""Serialize connector writers for one installation identity (Linux and macOS)."""
from __future__ import annotations
import fcntl
import os
from pathlib import Path
import stat
import sys

LOCK_FD = 'ATI_CONNECTOR_INSTALL_LOCK_FD'


def acquire(directory: Path) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / '.connector-install.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('installer lock is not a regular file')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


def inherited(directory: Path) -> bool:
    value = os.environ.get(LOCK_FD)
    if value is None:
        return False
    try:
        fd = int(value)
        if fd < 3:
            raise ValueError('invalid lock descriptor')
        actual = os.fstat(fd)
        expected = (directory / '.connector-install.lock').lstat()
        if (not stat.S_ISREG(actual.st_mode) or not stat.S_ISREG(expected.st_mode)
                or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)):
            raise ValueError('installer lock identity mismatch')
        # This succeeds for the inherited open-file description, but cannot
        # steal the lock when an unrelated process owns a separate descriptor.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (ValueError, OSError):
        raise ValueError('unverified installer lock descriptor') from None


def main() -> int:
    try:
        directory = Path(sys.argv[1]).expanduser().resolve()
        command = sys.argv[2:]
        if command == ['--check']:
            if not inherited(directory):
                raise ValueError('missing inherited lock')
            return 0
        if not command:
            raise ValueError('missing installer command')
        if not inherited(directory):
            fd = acquire(directory)
            os.set_inheritable(fd, True)
            os.environ[LOCK_FD] = str(fd)
        os.execvp(command[0], command)
    except (ValueError, OSError):
        print('Connector installation lock unavailable or invalid; no writer changed.', file=sys.stderr)
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
