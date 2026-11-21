"""可注入时钟。

业务代码一律通过 ``clock.now()`` 取当前时间，测试里用 ``clock.freeze()``
把时间钉死，避免真的等待 15 天。
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator, Optional

_provider: Optional[Callable[[], datetime]] = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now() -> datetime:
    """当前时间（UTC，带时区）。"""
    if _provider is not None:
        return _provider()
    return utcnow()


def set_now_provider(provider: Optional[Callable[[], datetime]]) -> None:
    global _provider
    _provider = provider


def reset() -> None:
    set_now_provider(None)


@contextlib.contextmanager
def freeze(at: datetime) -> Iterator[None]:
    """把时钟冻结在 ``at``（支持 `advance` 式推进）。"""
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)

    box = {"t": at}

    def _p() -> datetime:
        return box["t"]

    set_now_provider(_p)
    try:
        yield box
    finally:
        reset()


def advance_to(box: dict, delta: timedelta) -> datetime:
    """配合 freeze 使用：把冻结时间往前推。"""
    box["t"] = box["t"] + delta
    return box["t"]
