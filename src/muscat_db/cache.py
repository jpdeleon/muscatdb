import time
import threading
from collections import OrderedDict
from functools import wraps


class LRUCache:
    """Thread-safe mapping bounded to ``maxsize`` entries with LRU eviction.

    Backs the manually-managed web caches (rendered-page HTML, catalog
    lookups) that used to be plain module-level dicts: unbounded — a slow
    memory leak over a long-lived server — and mutated from FastAPI's
    threadpool (``def`` routes run off-thread) with no lock, so concurrent
    requests could corrupt the dict.

    Each method takes the lock for its whole critical section, so reads and
    writes are atomic; ``get`` distinguishes "absent" from a cached ``None``
    via the ``default`` sentinel rather than the racey ``key in cache`` /
    ``cache[key]`` two-step the callers previously used.
    """

    def __init__(self, maxsize: int = 128):
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.maxsize = maxsize
        self._data: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key, default=None):
        """Return the value for *key*, marking it most-recently-used; *default*
        (any sentinel the caller picks) when the key is absent."""
        with self._lock:
            try:
                self._data.move_to_end(key)
                return self._data[key]
            except KeyError:
                return default

    def __contains__(self, key) -> bool:
        with self._lock:
            return key in self._data

    def __getitem__(self, key):
        with self._lock:
            self._data.move_to_end(key)
            return self._data[key]

    def __setitem__(self, key, value) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)  # evict least-recently-used

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


class TTLCache:
    """Time-bounded memoizing decorator with an LRU size cap.

    Entries expire ``ttl`` seconds after insertion; independently, the cache
    never holds more than ``maxsize`` live entries (least-recently-used evicted
    first), so a long-running server can't accumulate stale keys for every
    distinct (inst, date, target) ever queried before any of them expire.
    """

    def __init__(self, ttl: float = 300.0, maxsize: int = 2048):
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.ttl = ttl
        self.maxsize = maxsize
        self.cache: OrderedDict = OrderedDict()
        self.lock = threading.Lock()

    def __call__(self, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.time()
            with self.lock:
                if key in self.cache:
                    val, expiry = self.cache[key]
                    if now < expiry:
                        self.cache.move_to_end(key)
                        return val
                    del self.cache[key]
            val = func(*args, **kwargs)
            with self.lock:
                self.cache[key] = (val, now + self.ttl)
                self.cache.move_to_end(key)
                while len(self.cache) > self.maxsize:
                    self.cache.popitem(last=False)  # evict least-recently-used
            return val

        wrapper.cache_clear = self.clear
        return wrapper

    def clear(self):
        with self.lock:
            self.cache.clear()

_registered_caches: list[TTLCache] = []

def register_cache(ttl: float = 300.0, maxsize: int = 2048) -> TTLCache:
    cache = TTLCache(ttl, maxsize)
    _registered_caches.append(cache)
    return cache

def clear_all_caches() -> None:
    for cache in _registered_caches:
        cache.clear()
