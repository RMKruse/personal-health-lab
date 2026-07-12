"""Intent-oriented local storage interface."""

from ._store import LocalStore, StoreError

__all__ = ["LocalStore", "StoreError"]
