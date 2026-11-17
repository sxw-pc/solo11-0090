"""Pagination collectors."""
from .base import BaseCollector, CollectorError, collect_strategy, get_collector
from .keyset import KeysetCollector
from .offset import OffsetCollector
from .pagenum import PageNumberCollector
from .snapshot import SnapshotCollector
__all__ = [
    "BaseCollector",
    "CollectorError",
    "KeysetCollector",
    "OffsetCollector",
    "PageNumberCollector",
    "SnapshotCollector",
    "collect_strategy",
    "get_collector",
]
