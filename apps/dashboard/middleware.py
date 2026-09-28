"""Header `Server-Timing` (jumlah & durasi query DB + total waktu server) di setiap respons
untuk pengguna yang sudah login -- supaya dampak optimasi bisa diukur langsung di DevTools
(Network -> Timing), juga di ZimaOS, tanpa DEBUG. Pengunjung anonim tidak mendapatkannya."""

from __future__ import annotations

import time

from django.db import connection


class ServerTimingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        stats = {"n": 0, "db": 0.0}

        def count(execute, sql, params, many, context):
            t = time.perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                stats["n"] += 1
                stats["db"] += time.perf_counter() - t

        start = time.perf_counter()
        with connection.execute_wrapper(count):
            response = self.get_response(request)
        total_ms = (time.perf_counter() - start) * 1000

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            response["Server-Timing"] = (
                f'db;desc="{stats["n"]} query";dur={stats["db"] * 1000:.1f}, app;desc="Django";dur={total_ms:.1f}'
            )
        return response
