import time
import threading
from functools import wraps

class TTLCache:
    def __init__(self, ttl: float = 300.0):
        self.ttl = ttl
        self.cache = {}
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
                        return val
                    del self.cache[key]
            val = func(*args, **kwargs)
            with self.lock:
                self.cache[key] = (val, now + self.ttl)
            return val
        
        wrapper.cache_clear = self.clear
        return wrapper

    def clear(self):
        with self.lock:
            self.cache.clear()

_registered_caches: list[TTLCache] = []

def register_cache(ttl: float = 300.0) -> TTLCache:
    cache = TTLCache(ttl)
    _registered_caches.append(cache)
    return cache

def clear_all_caches() -> None:
    for cache in _registered_caches:
        cache.clear()
