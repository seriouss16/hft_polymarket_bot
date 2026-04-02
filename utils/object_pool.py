"""Object pool for reusing frequently allocated objects and reducing GC pressure."""

from __future__ import annotations

import logging
from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")


class ObjectPool(Generic[T]):
    """Simple object pool for reusing objects instead of frequent allocations.

    The pool maintains a collection of pre-allocated objects that can be borrowed
    and returned. This reduces GC pressure in hot paths where many short-lived
    objects are created (e.g., per-tick data structures).

    Attributes:
        max_size: Maximum number of objects to keep in the pool (default 100)
        _factory: Callable that creates new objects when pool is empty
        _pool: Internal list of available objects
    """

    def __init__(self, factory: Callable[[], T], max_size: int = 100) -> None:
        """Initialize the object pool.

        Args:
            factory: Function that creates new objects of type T
            max_size: Maximum pool size (default 100)
        """
        self.max_size = max_size
        self._factory = factory
        self._pool: list[T] = []

    def borrow(self) -> T:
        """Borrow an object from the pool.

        If pool has objects, returns one from the pool.
        If pool is empty, creates a new object using factory.

        Returns:
            An object of type T
        """
        if self._pool:
            return self._pool.pop()
        return self._factory()

    def return_obj(self, obj: T) -> None:
        """Return an object to the pool.

        If pool is not full, adds the object back for reuse.
        If pool is full, the object is discarded (will be garbage collected).

        Args:
            obj: The object to return to the pool
        """
        if len(self._pool) < self.max_size:
            self._pool.append(obj)

    def clear(self) -> None:
        """Clear all objects from the pool."""
        self._pool.clear()

    def size(self) -> int:
        """Return current number of objects in the pool."""
        return len(self._pool)
