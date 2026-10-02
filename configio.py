"""Atomic private configuration writes; serialization across client processes."""
from __future__ import annotations
import os
import re
import tempfile
import time
from contextlib import contextmanager


class LockBusyError(TimeoutError):
    """Lock was not acquired before the caller's deadline."""


@contextmanager
def file_lock(path, timeout=None):
    import fcntl
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd = os.open(path + '.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        if timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + max(0,timeout)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        raise LockBusyError('ресурс занят другим клиентом; операция не начата') from None
                    time.sleep(min(.05,remaining))
        yield
    finally:
        os.close(fd)


def atomic_text(path, text):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.uachat-', dir=parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_value(path, name, value):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) or any(c in value for c in '\r\n\x00'):
        raise ValueError('invalid configuration key/value')
    with file_lock(path):
        try:
            with open(path, encoding='utf-8') as stream:
                lines = stream.read().splitlines()
        except FileNotFoundError:
            lines = []
        result = [line for line in lines if line.partition('=')[0].strip() != name]
        result.append(f'{name}={value}')
        atomic_text(path, '\n'.join(result)+'\n')
