"""Prometheus metrics. Standard HTTP request metrics (rate, latency, status)
come free from prometheus-fastapi-instrumentator (see app.main); the
counters in metrics.py are the platform-specific ones that library can't
know about - LLM/embedding provider calls, cache hit rate, spend-budget
denials, investigations created.
"""
