"""线程安全的错误统计收集。"""

import errno
import socket
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

from .config import ERROR_HISTORY_SIZE


class ErrorStats:
    """
    线程安全的错误统计收集器。
    分类记录错误类型、频率、最近消息，用于调试和状态报告。
    """
    ERROR_CATEGORIES = {
        'socket_error':     "Socket 通用错误",
        'buffer_overflow':  "缓冲区溢出 (EAGAIN/ENOBUFS)",
        'permission_denied':"权限不足 (EACCES/EPERM)",
        'network_unreachable': "网络不可达",
        'connection_refused':  "连接被拒绝",
        'timeout':          "操作超时",
        'invalid_argument': "无效参数",
        'too_many_files':   "文件描述符耗尽",
        'unknown':          "未知错误",
    }

    def __init__(self, max_history: int = ERROR_HISTORY_SIZE):
        self._lock = threading.Lock()
        self._counts: dict = {k: 0 for k in self.ERROR_CATEGORIES}
        self._counts['total'] = 0
        self._recent: deque = deque(maxlen=max_history)
        self._first_error_time: Optional[float] = None
        self._last_error_time: Optional[float] = None

    def record(self, category: str, message: str, errno_val: Optional[int] = None):
        """记录一条错误"""
        with self._lock:
            if category not in self._counts:
                category = 'unknown'
            self._counts[category] += 1
            self._counts['total'] += 1
            now = time.monotonic()
            if self._first_error_time is None:
                self._first_error_time = now
            self._last_error_time = now
            self._recent.append({
                'time': now,
                'category': category,
                'message': message,
                'errno': errno_val,
            })

    def record_exception(self, e: Exception, context: str = ""):
        """从异常对象自动分类并记录"""
        err_no = getattr(e, 'errno', None)
        msg = f"{context}: {e}" if context else str(e)

        # 按 errno 分类
        if err_no in (errno.EAGAIN, errno.EWOULDBLOCK):
            self.record('buffer_overflow', f"EAGAIN/EWOULDBLOCK - {msg}", err_no)
        elif err_no == errno.ENOBUFS:
            self.record('buffer_overflow', f"ENOBUFS - {msg}", err_no)
        elif err_no in (errno.EACCES, errno.EPERM):
            self.record('permission_denied', f"权限不足 - {msg}", err_no)
        elif err_no in (errno.ENETUNREACH, errno.EHOSTUNREACH):
            self.record('network_unreachable', f"网络不可达 - {msg}", err_no)
        elif err_no == errno.ECONNREFUSED:
            self.record('connection_refused', f"连接被拒 - {msg}", err_no)
        elif err_no == errno.EINVAL:
            self.record('invalid_argument', f"无效参数 - {msg}", err_no)
        elif err_no == errno.EMFILE:
            self.record('too_many_files', f"FD 耗尽 - {msg}", err_no)
        elif isinstance(e, socket.error):
            self.record('socket_error', f"Socket - {msg}", err_no)
        else:
            self.record('unknown', f"未知 - {msg}", err_no)

    def get_summary(self) -> str:
        """返回人类可读的错误摘要"""
        with self._lock:
            if self._counts['total'] == 0:
                return "  ✅ 无错误"
            lines = [f"  📊 错误总数: {self._counts['total']}"]
            for cat, desc in self.ERROR_CATEGORIES.items():
                cnt = self._counts.get(cat, 0)
                if cnt > 0:
                    lines.append(f"     {desc}: {cnt} 次")
            return "\n".join(lines)

    def get_recent(self, n: int = 5) -> str:
        """返回最近 n 条错误详情"""
        with self._lock:
            if not self._recent:
                return "  (无)"
            items = list(self._recent)[-n:]
            lines = []
            for item in items:
                ts = datetime.fromtimestamp(time.time() - (time.monotonic() - item['time']))
                lines.append(f"  [{ts.strftime('%H:%M:%S')}] {item['category']}: {item['message']}")
            return "\n".join(lines)

    def should_stop_thread(self) -> bool:
        """判断某类致命错误是否过多，线程应自毁"""
        with self._lock:
            # 权限错误超过 3 次 → 停
            if self._counts.get('permission_denied', 0) >= 3:
                return True
            # 网络不可达超过 5 次 → 停
            if self._counts.get('network_unreachable', 0) >= 5:
                return True
            return False

    def total(self) -> int:
        return self._counts.get('total', 0)


# 全局错误统计实例
error_stats = ErrorStats()
