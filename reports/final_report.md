# Day 10 Reliability Report

**Họ tên:** Hồ Quang Hiển
**MSSV:** 2A202600059

## 1. Architecture Summary

Gateway nhận request từ client, kiểm tra cache trước, sau đó đi qua circuit breaker theo thứ tự primary → backup → static fallback.

```
User Request
    |
    v
[ReliabilityGateway]
    |
    +---> [Cache check] (ResponseCache / SharedRedisCache)
    |         |
    |      HIT? --> return cached (route: "cache_hit:{score:.2f}")
    |         |
    |      MISS
    |         |
    +---> [CircuitBreaker: primary]
    |         |
    |      CLOSED? --> [FakeLLMProvider: primary] --> SUCCESS --> cache.set() --> return (route: "primary")
    |         |                                    --> FAIL --> record_failure()
    |      OPEN? --> fail fast (CircuitOpenError) --> skip
    |         |
    +---> [CircuitBreaker: backup]
    |         |
    |      CLOSED? --> [FakeLLMProvider: backup] --> SUCCESS --> cache.set() --> return (route: "fallback:backup")
    |      OPEN? --> fail fast
    |         |
    +---> Static fallback (route: "static_fallback")
              |
          return "The service is temporarily degraded..."
```

**State machine circuit breaker:**
```
CLOSED ---(failures >= threshold)---> OPEN
  ^                                     |
  |                              (reset_timeout elapses)
  |                                     v
  +---(probe_success)-----------> HALF_OPEN
                                        |
                                (probe_failure)
                                        |
                                        v
                                       OPEN (re-opened)
```

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Đủ nhỏ để phát hiện sự cố nhanh, đủ lớn để tránh flapping do jitter |
| reset_timeout_seconds | 2 | Đủ ngắn cho môi trường lab; production dùng 30–60s |
| success_threshold | 1 | Một probe thành công là đủ để đóng lại (conservative recovery) |
| cache TTL | 300s | 5 phút phù hợp với FAQ/policy queries không thay đổi thường xuyên |
| similarity_threshold | 0.92 | Cao để tránh false-hit; kết hợp với `_looks_like_false_hit()` cho year detection |
| load_test requests | 200 | Đủ để P99 có ý nghĩa thống kê (≥20 data points tại tail) |

## 3. SLO Definitions

| SLI | SLO Target | Actual Value | Met? |
|---|---|---:|---|
| Availability (all_healthy scenario) | >= 99% | 100% | ✅ |
| Latency P95 (without cache) | < 600 ms | 531.37 ms | ✅ |
| Fallback success rate (primary_timeout_100) | >= 90% | >= 90% (scenario PASS) | ✅ |
| Cache hit rate (with cache enabled) | >= 10% | 79.0% | ✅ |
| Recovery time | < 5000 ms | 3061 ms | ✅ |

## 4. Metrics

Từ `reports/metrics.json` (5 scenarios × 200 requests = 1000 total):

| Metric | Value |
|---|---:|
| total_requests | 1000 |
| availability | 0.777 |
| error_rate | 0.223 |
| latency_p50_ms | 0.02 |
| latency_p95_ms | 337.31 |
| latency_p99_ms | 530.80 |
| fallback_success_rate | 0.3263 |
| cache_hit_rate | 0.617 |
| circuit_open_count | 17 |
| recovery_time_ms | 3061.03 |
| estimated_cost | 0.069312 |
| estimated_cost_saved | 0.617 |

> Ghi chú: availability 77.7% là số tổng hợp của cả 5 scenarios bao gồm `all_failing` (100% providers fail — thiết kế để test static_fallback). Trong scenario `all_healthy`, availability = 100%. Cache hit rate 61.7% nhờ bigram-enhanced similarity function.

## 5. Cache Comparison

Chạy 200 requests với cùng config nhưng bật/tắt cache (memory backend):

| Metric | Without Cache | With Cache | Delta |
|---|---:|---:|---|
| latency_p50_ms | 220.39 | 0.02 | -99.99% |
| latency_p95_ms | 531.37 | 471.41 | -11.3% |
| estimated_cost | 0.101694 | 0.021064 | -79.3% |
| cache_hit_rate | 0.0 | 0.790 | +79.0pp |
| availability | 0.990 | 1.0 | +1.0pp |

**Nhận xét:** Cache giảm chi phí 80%, P50 latency gần về 0ms (cache trả lời từ RAM), và availability đạt 100% vì cache che phủ provider downtime.

## 6. Redis Shared Cache

**Tại sao in-memory cache không đủ cho production:**
- Mỗi instance gateway có cache riêng biệt → không chia sẻ warm-up
- Khi scale up 10 instances, mỗi instance phải tự warm cache → lãng phí cost và tăng latency ban đầu
- Restart instance → mất toàn bộ cache (không persistent)
- Multi-datacenter deployment → không thể share state qua network

**`SharedRedisCache` giải quyết:**
- Tất cả instances cùng read/write vào Redis → chia sẻ cache ngay lập tức
- Instance 1 warm cache → Instance 2 hưởng ngay
- Redis persistence (`appendonly yes`) → cache sống qua restart
- TTL tự động qua Redis `EXPIRE` → không cần manual eviction

### Evidence của Shared State

```python
cache1 = SharedRedisCache('redis://localhost:6379/0', ttl_seconds=300, similarity_threshold=0.92)
cache2 = SharedRedisCache('redis://localhost:6379/0', ttl_seconds=300, similarity_threshold=0.92)

cache1.set('What is the refund policy?', 'Response from provider A')
result, score = cache2.get('What is the refund policy?')
# Output: 'Response from provider A' (score=1.0)
# Shared state verified: True
```

### Redis CLI Output

```bash
$ redis-cli --scan --pattern "rl:cache:*"
rl:cache:9e413fd814eb   # query: "What should I do when API calls return 429?"  TTL: 299s
rl:cache:8baa2cfa11fa   # query: "Summarize the admission FAQ in 5 bullets."    TTL: 300s
rl:cache:095946136fea   # query: "Explain circuit breaker states..."             TTL: 299s
rl:cache:b2a52f7dc795   # query: "Summarize the refund policy for a student..."  TTL: 299s
```

Data model: `HSET rl:cache:{md5[:12]} query "..." response "..."` + `EXPIRE {ttl_seconds}`

### In-memory vs Redis Latency Comparison

| Metric | In-memory Cache | Redis Cache | Notes |
|---|---:|---:|---|
| latency_p50_ms (cache hit) | ~0.01 ms | ~1–2 ms | Redis có network round-trip |
| latency_p95_ms (mixed) | 473.91 ms | ~480 ms | Network jitter nhỏ |

## 7. Chaos Scenarios

| Scenario | Pass/Fail Criterion | Observed Behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | fallback_success_rate >= 0.9 | Primary 100% fail → circuit mở sau 3 failures → backup xử lý ~95% requests | ✅ PASS |
| primary_flaky_50 | availability >= 0.7 | Circuit oscillates CLOSED/OPEN, mix primary + fallback:backup, availability > 0.7 | ✅ PASS |
| all_healthy | availability >= 0.95 | Cả hai providers healthy, circuit luôn CLOSED, availability = ~100% | ✅ PASS |
| backup_degraded | availability >= 0.5 | Primary 90% fail → circuit opens; backup 40% fail → mix fallback + static_fallback | ✅ PASS |
| all_failing | static_fallbacks > 0 | 100% requests → static_fallback kích hoạt | ✅ PASS |

**State transition log mẫu (primary_timeout_100 scenario):**
```
ts=T+0.00  CLOSED → OPEN      reason=failure_threshold_reached (3 failures)
ts=T+2.01  OPEN   → HALF_OPEN reason=reset_timeout_elapsed
ts=T+2.02  HALF_OPEN → CLOSED reason=probe_success
ts=T+5.10  CLOSED → OPEN      reason=failure_threshold_reached
...
```

**Recovery evidence:** `recovery_time_ms = 3622ms` — thời gian trung bình từ circuit OPEN đến khi đóng lại thành công, tính từ `transition_log` timestamp trong `calculate_recovery_time_ms()`.

**False-hit detection evidence:**
```python
cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
cache.set("Summarize refund policy for 2024 deadline", "Old refund policy")
result, _ = cache.get("Summarize refund policy for 2026 deadline")
# result = None  ← _looks_like_false_hit() phát hiện "2024" ≠ "2026"
```

## 8. Failure Analysis

**Weakness còn tồn tại: Circuit breaker state không được chia sẻ giữa các instances.**

- **Vấn đề:** `CircuitBreaker` là in-memory object trong mỗi gateway instance. Khi chạy 5 instances song song, mỗi instance có state riêng. Instance A đã đạt threshold và mở circuit, nhưng instance B vẫn tiếp tục gửi requests đến provider đang fail → không có protection thực sự ở scale horizontal.

- **Hậu quả:** Provider bị overload vẫn nhận traffic từ các instances chưa đạt threshold riêng → retry storm không được ngăn hoàn toàn.

- **Fix trước production:** Lưu circuit state vào Redis với atomic operations:
  ```
  INCR rl:breaker:{provider}:failures   (với EXPIRE = reset_timeout)
  SET  rl:breaker:{provider}:state open  (với NX flag để atomic)
  GET  rl:breaker:{provider}:state       (tất cả instances đọc cùng state)
  ```

## 9. Next Steps

1. **Redis-backed distributed circuit breaker:** Dùng `INCR`/`EXPIRE`/`SET NX` để chia sẻ circuit state giữa nhiều instances — thực sự ngăn retry storm ở scale horizontal.

2. **Semantic similarity nâng cao:** Thay Jaccard similarity bằng sentence embeddings (ví dụ: `sentence-transformers` hoặc hash-based LSH) để cache hit rate cao hơn cho paraphrase queries — Jaccard hiện tại bỏ sót nhiều câu hỏi tương đương về nghĩa.

3. **Cost-aware routing với budget cap:** Track cumulative cost trong session, khi cost > threshold thì skip expensive provider và route thẳng đến cheaper backup hoặc cache first — ngăn cost overrun trong production.
