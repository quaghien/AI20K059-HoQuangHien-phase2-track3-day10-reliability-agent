from __future__ import annotations

import copy
import json
import random
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs.

    Recovery time = time between circuit opening and next successful close.
    Returns the average recovery time across all breakers, or None if no recovery occurred.
    """
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open" and open_ts is None:
                open_ts = float(entry["ts"])
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((float(entry["ts"]) - open_ts) * 1000)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario."""
    gateway = build_gateway(config, scenario.provider_overrides or None)
    metrics = RunMetrics()
    request_count = config.load_test.requests
    for _ in range(request_count):
        prompt = random.choice(queries)
        result = gateway.complete(prompt)
        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost
        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += 0.001
        if result.route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        elif result.route.startswith("fallback:"):
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        else:
            # primary hoặc cache_hit
            metrics.successful_requests += 1
        if result.latency_ms:
            metrics.latencies_ms.append(result.latency_ms)

    metrics.circuit_open_count = sum(
        1 for breaker in gateway.breakers.values() for t in breaker.transition_log if t["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics


SCENARIO_CRITERIA: dict[str, object] = {
    # primary_timeout_100: primary 100% fail → backup phải xử lý, fallback rate >= 0.9
    "primary_timeout_100": lambda r: r.fallback_success_rate >= 0.9,
    # primary_flaky_50: primary fail 50% → circuit mở, availability > 0.7
    "primary_flaky_50": lambda r: r.availability >= 0.7,
    # all_healthy: cả hai healthy → availability >= 0.95
    "all_healthy": lambda r: r.availability >= 0.95,
    # backup_degraded: cả hai bị suy giảm → vẫn phải serve được >= 50%
    "backup_degraded": lambda r: r.availability >= 0.5,
    # all_failing: tất cả fail → static_fallback phải kích hoạt
    "all_failing": lambda r: r.static_fallbacks > 0,
}


def _evaluate_scenario(name: str, result: RunMetrics) -> str:
    criterion = SCENARIO_CRITERIA.get(name)
    if criterion is None:
        return "pass" if result.successful_requests > 0 else "fail"
    passed: bool = bool(criterion(result))  # type: ignore[operator]
    return "pass" if passed else "fail"


def run_cache_comparison(config: LabConfig, queries: list[str]) -> dict[str, object]:
    """So sánh metrics với cache bật và tắt."""
    results: dict[str, object] = {}
    for cache_enabled in [False, True]:
        cfg = copy.deepcopy(config)
        cfg.cache.enabled = cache_enabled
        label = "with_cache" if cache_enabled else "without_cache"
        scenario = ScenarioConfig(name=label, description="cache comparison")
        r = run_scenario(cfg, queries, scenario)
        results[label] = {
            "latency_p50_ms": round(r.percentile(50), 2),
            "latency_p95_ms": round(r.percentile(95), 2),
            "estimated_cost": round(r.estimated_cost, 6),
            "cache_hit_rate": round(r.cache_hit_rate, 4),
            "availability": round(r.availability, 4),
        }
    return results


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined."""
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": _evaluate_scenario("default", metrics)}
        return metrics

    combined = RunMetrics()
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)
        combined.scenarios[scenario.name] = _evaluate_scenario(scenario.name, result)

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    return combined
