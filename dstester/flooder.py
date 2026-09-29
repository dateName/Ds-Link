"""UDP 发送线程。"""

import errno
import socket
import threading
import time
from typing import Callable, Optional

from .config import DEFAULT_SNDBUF, MAX_SNDBUF, MIN_SNDBUF
from .errors import error_stats


class UdpFlooder(threading.Thread):
    """
    改进版 UDP 发送线程：
    - 分类错误处理，不再静默 break
    - 缓冲区溢出时指数退避重试
    - 支持随机端口 / 端口范围
    - 可配置 SO_SNDBUF
    - 发送速率统计
    """

    # 致命错误 errno 集合（遇到直接退出线程）
    FATAL_ERRORS = {
        errno.EACCES, errno.EPERM, errno.ENETUNREACH, errno.EHOSTUNREACH,
        10049,  # Windows WSAEADDRNOTAVAIL: 源地址不属于本机
    }

    def __init__(
        self,
        host: str,
        port_func: Callable[[], int],       # 返回端口的回调函数（支持随机）
        payload_func: Callable[[], bytes],  # 返回 payload 的回调函数
        stop_event: threading.Event,
        sndbuf_size: int = DEFAULT_SNDBUF,
        thread_id: int = 0,
        local_ip: str = "",
    ):
        super().__init__(daemon=True)
        self.host = host
        self.port_func = port_func
        self.payload_func = payload_func
        self.stop_event = stop_event
        self.sndbuf_size = max(MIN_SNDBUF, min(sndbuf_size, MAX_SNDBUF))
        self.thread_id = thread_id
        self.local_ip = local_ip

        # 统计
        self.packets_sent = 0
        self.bytes_sent = 0
        self.send_errors = 0
        self.consecutive_errors = 0
        self.backoff_until = 0.0

    def _create_socket(self) -> Optional[socket.socket]:
        """创建并配置 socket"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self.sndbuf_size)
            except OSError as e:
                error_stats.record_exception(e, f"Thread-{self.thread_id} SO_SNDBUF 设置失败")
            if self.local_ip:
                sock.bind((self.local_ip, 0))
            return sock
        except OSError as e:
            error_stats.record_exception(e, f"Thread-{self.thread_id} socket 创建/绑定失败")
            return None

    def run(self):
        sock = self._create_socket()
        if sock is None:
            return

        while not self.stop_event.is_set():
            # 指数退避期间，sleep 让出 CPU
            now = time.monotonic()
            if now < self.backoff_until:
                time.sleep(min(0.1, self.backoff_until - now))
                continue

            port = self.port_func()
            addr = (self.host, port)

            try:
                payload = self.payload_func()
                sock.sendto(payload, addr)
                self.packets_sent += 1
                self.bytes_sent += len(payload)
                self.consecutive_errors = 0  # 重置连续错误计数
            except OSError as e:
                self.send_errors += 1
                self.consecutive_errors += 1
                err_no = getattr(e, 'errno', None)

                if err_no in self.FATAL_ERRORS:
                    # 致命错误：记录并退出线程
                    error_stats.record_exception(e, f"Thread-{self.thread_id} 致命错误")
                    break
                elif err_no in (errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOBUFS):
                    # 缓冲区满：指数退避
                    error_stats.record_exception(e, f"Thread-{self.thread_id} 缓冲区满")
                    backoff = min(0.5, 0.001 * (2 ** min(self.consecutive_errors, 9)))
                    self.backoff_until = time.monotonic() + backoff
                else:
                    # 其他错误
                    error_stats.record_exception(e, f"Thread-{self.thread_id}")
                    if self.consecutive_errors >= 20:
                        break
            except Exception as e:
                self.send_errors += 1
                error_stats.record_exception(e, f"Thread-{self.thread_id} 未知异常")
                break

        try:
            sock.close()
        except:
            pass

    def stats_str(self) -> str:
        mb = self.bytes_sent / (1024 * 1024)
        return f"Thread-{self.thread_id}: {self.packets_sent} 包 / {mb:.1f}MB / {self.send_errors} 错误"
