"""Tests for bounded, thread-safe caches (architecture audit M1).

LRUCache backs the web layer's previously-unbounded, unlocked module dicts
(_CATALOG_CACHE / _index_cache); TTLCache gains an LRU size cap so it can't
accumulate a stale entry per distinct (inst, date, target) before any expire.
"""

import threading
import time

import pytest

from muscat_db.cache import LRUCache, TTLCache, register_cache


class TestLRUCacheBounded:
    def test_get_set_roundtrip(self):
        c = LRUCache(maxsize=4)
        c["a"] = 1
        assert c.get("a") == 1
        assert c["a"] == 1
        assert "a" in c

    def test_evicts_least_recently_used_when_over_maxsize(self):
        c = LRUCache(maxsize=2)
        c["a"] = 1
        c["b"] = 2
        c["c"] = 3  # exceeds cap -> oldest ("a") evicted
        assert "a" not in c
        assert "b" in c and "c" in c
        assert len(c) == 2

    def test_access_refreshes_recency(self):
        c = LRUCache(maxsize=2)
        c["a"] = 1
        c["b"] = 2
        assert c.get("a") == 1  # "a" now most-recently-used
        c["c"] = 3  # evicts the now-oldest, "b"
        assert "a" in c
        assert "b" not in c
        assert "c" in c

    def test_setitem_refreshes_recency(self):
        c = LRUCache(maxsize=2)
        c["a"] = 1
        c["b"] = 2
        c["a"] = 10  # re-write bumps "a" to most-recent
        c["c"] = 3   # evicts "b"
        assert c.get("a") == 10
        assert "b" not in c

    def test_get_default_sentinel_distinguishes_absent_from_cached_none(self):
        c = LRUCache(maxsize=4)
        miss = object()
        assert c.get("absent", miss) is miss
        c["present"] = None  # legitimately cached None
        assert c.get("present", miss) is None  # not the miss sentinel

    def test_clear_empties(self):
        c = LRUCache(maxsize=4)
        c["a"] = 1
        c.clear()
        assert len(c) == 0
        assert "a" not in c

    def test_maxsize_must_be_positive(self):
        with pytest.raises(ValueError):
            LRUCache(maxsize=0)

    def test_concurrent_writes_stay_bounded_and_uncorrupted(self):
        c = LRUCache(maxsize=50)

        def worker(base):
            for i in range(500):
                c[f"{base}:{i}"] = i

        threads = [threading.Thread(target=worker, args=(b,)) for b in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Never exceeds the cap despite 4000 concurrent insertions.
        assert len(c) == 50


class TestTTLCacheBounded:
    def test_caches_within_ttl(self):
        calls = []

        @TTLCache(ttl=100.0, maxsize=8)
        def f(x):
            calls.append(x)
            return x * 2

        assert f(3) == 6
        assert f(3) == 6
        assert calls == [3]  # second call served from cache

    def test_expiry_recomputes(self):
        calls = []

        @TTLCache(ttl=0.05, maxsize=8)
        def f(x):
            calls.append(x)
            return x

        f(1)
        time.sleep(0.06)
        f(1)
        assert calls == [1, 1]  # expired -> recomputed

    def test_lru_size_cap_evicts_oldest(self):
        calls = []

        @TTLCache(ttl=1000.0, maxsize=2)
        def f(x):
            calls.append(x)
            return x

        f(1)
        f(2)
        f(3)          # evicts key for f(1)
        f(1)          # must recompute (was evicted)
        f(3)          # still cached
        assert calls == [1, 2, 3, 1]

    def test_hit_refreshes_recency(self):
        calls = []

        @TTLCache(ttl=1000.0, maxsize=2)
        def f(x):
            calls.append(x)
            return x

        f(1)
        f(2)
        f(1)          # bump 1 to most-recent
        f(3)          # evicts 2 (now oldest), not 1
        f(1)          # cached
        f(2)          # recompute
        assert calls == [1, 2, 3, 2]

    def test_cache_clear_attribute(self):
        @TTLCache(ttl=1000.0, maxsize=8)
        def f(x):
            return object()

        a = f(1)
        f.cache_clear()
        assert f(1) is not a  # cleared -> fresh value

    def test_register_cache_accepts_maxsize(self):
        c = register_cache(ttl=10.0, maxsize=3)
        assert isinstance(c, TTLCache)
        assert c.maxsize == 3
