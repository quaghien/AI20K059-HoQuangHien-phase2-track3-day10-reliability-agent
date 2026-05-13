from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """In-memory LRU cache with TTL, privacy guardrails, and false-hit detection."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            return None, 0.0
        best_value: str | None = None
        best_score = 0.0
        best_key: str | None = None
        now = time.time()
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]
        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_value = entry.value
                best_key = entry.key
        if best_score >= self.similarity_threshold:
            if best_key and _looks_like_false_hit(query, best_key):
                return None, best_score
            return best_value, best_score
        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        if _is_uncacheable(query):
            return
        self._entries.append(CacheEntry(query, value, time.time(), metadata or {}))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Token-overlap (Jaccard) similarity with bigram boost."""
        tokens_a = a.lower().split()
        tokens_b = b.lower().split()
        if not tokens_a or not tokens_b:
            return 0.0
        unigrams_a = set(tokens_a)
        unigrams_b = set(tokens_b)
        unigram_score = len(unigrams_a & unigrams_b) / len(unigrams_a | unigrams_b)
        # Bigrams capture phrase-level similarity
        bigrams_a = {(tokens_a[i], tokens_a[i + 1]) for i in range(len(tokens_a) - 1)}
        bigrams_b = {(tokens_b[i], tokens_b[i + 1]) for i in range(len(tokens_b) - 1)}
        if bigrams_a or bigrams_b:
            bigram_score = len(bigrams_a & bigrams_b) / len(bigrams_a | bigrams_b) if (bigrams_a | bigrams_b) else 0.0
            return 0.7 * unigram_score + 0.3 * bigram_score
        return unigram_score


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    Data model: HSET {prefix}{md5_hash} query "..." response "..." + EXPIRE {ttl}
    Gracefully degrades to cache-miss when Redis is unreachable.
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis. Returns (None, 0.0) on any Redis error."""
        if _is_uncacheable(query):
            return None, 0.0
        try:
            # Exact match trước
            key = f"{self.prefix}{self._query_hash(query)}"
            response = self._redis.hget(key, "response")
            if response is not None:
                return response, 1.0

            # Similarity scan toàn bộ keys
            best_value: str | None = None
            best_score = 0.0
            best_cached_query: str | None = None
            for redis_key in self._redis.scan_iter(f"{self.prefix}*"):
                cached_query = self._redis.hget(redis_key, "query")
                if cached_query is None:
                    continue
                score = ResponseCache.similarity(query, cached_query)
                if score > best_score:
                    best_score = score
                    best_value = self._redis.hget(redis_key, "response")
                    best_cached_query = cached_query

            if best_score >= self.similarity_threshold:
                if best_cached_query and _looks_like_false_hit(query, best_cached_query):
                    self.false_hit_log.append(
                        {"query": query, "cached_key": best_cached_query, "score": best_score}
                    )
                    return None, best_score
                return best_value, best_score
        except Exception:
            # Graceful degradation: Redis không khả dụng → cache miss
            pass
        return None, 0.0

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL. Silently skips on Redis error."""
        if _is_uncacheable(query):
            return
        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            self._redis.hset(key, mapping={"query": query, "response": value})
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            pass

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
