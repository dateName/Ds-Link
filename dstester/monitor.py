"""Ping 监测线程。"""

import threading
import time
from collections import deque

from .config import MAX_RESULTS_HISTORY
from .icmp import IcmpPing


class PingMonitor(threading.Thread):
    """
    改进版 Ping 监测：
    - 使用纯 Python ICMP 实现（优先）或系统 ping（回退）
    - 延迟结果用 deque 限制内存
    - 额外统计丢包率、抖动
    """

    def __init__(self, host: str, interval: float = 0.3, timeout: float = 1.0):
        super().__init__(daemon=True)
        self.host = host
        self.interval = interval
        self.timeout = timeout
        self.stop_event = threading.Event()
        self.results: deque = deque(maxlen=MAX_RESULTS_HISTORY)
        self._icmp = IcmpPing(host, timeout=timeout)
        self._last_ping_time = 0.0

    def run(self):
        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                # 精确间隔控制
                sleep_needed = self.interval - (now - self._last_ping_time)
                if sleep_needed > 0:
                    self.stop_event.wait(sleep_needed)
                    if self.stop_event.is_set():
                        break
                self._last_ping_time = time.monotonic()
                latency = self._icmp.ping()
                self.results.append(latency)
        finally:
            self._icmp.close()

    def stop(self):
        self.stop_event.set()

    def get_stats(self) -> dict:
        """返回当前统计快照"""
        valid = [l for l in self.results if l > 0]
        timeouts = sum(1 for l in self.results if l < 0)
        stats = {
            'total': len(self.results),
            'valid_count': len(valid),
            'timeout_count': timeouts,
            'timeout_rate': (timeouts / len(self.results) * 100) if self.results else 0,
            'current': valid[-1] if valid else -1,
            'avg': (sum(valid) / len(valid)) if valid else 0,
            'min': min(valid) if valid else 0,
            'max': max(valid) if valid else 0,
        }
        # 计算抖动（最近 10 个样本的平均偏差）
        if len(valid) >= 2:
            recent = list(self.results)[-10:]
            recent_valid = [r for r in recent if r > 0]
            if len(recent_valid) >= 2:
                diffs = [abs(recent_valid[i] - recent_valid[i-1]) for i in range(1, len(recent_valid))]
                stats['jitter'] = sum(diffs) / len(diffs)
            else:
                stats['jitter'] = 0
        else:
            stats['jitter'] = 0
        return stats
