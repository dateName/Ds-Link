"""跨平台 ICMP Ping 实现。"""

import os
import platform
import socket
import struct
import subprocess
import time
from typing import Optional

from .errors import error_stats


class IcmpPing:
    """
    纯 Python ICMP Echo 实现。
    - Linux: 使用 SOCK_DGRAM + IPPROTO_ICMP（无需 root）
    - macOS/Windows: 尝试 raw socket，失败则回退到系统 ping
    - 自动处理校验和计算
    """

    ICMP_ECHO_REQUEST = 8
    ICMP_ECHO_REPLY = 0

    def __init__(self, host: str, timeout: float = 1.0):
        self.host = host
        self.timeout = timeout
        self._seq = 0
        self._pid = os.getpid() & 0xFFFF
        self._sock: Optional[socket.socket] = None
        self._use_native = False
        self._init_socket()

    def _init_socket(self):
        """初始化 ICMP socket，失败则标记回退"""
        try:
            if platform.system().lower() == "linux":
                self._sock = socket.socket(
                    socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP
                )
            else:
                # macOS / Windows 需要 raw socket（通常需要 root/admin）
                self._sock = socket.socket(
                    socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP
                )
            self._sock.settimeout(self.timeout)
        except (PermissionError, OSError) as e:
            error_stats.record_exception(e, "ICMP socket 创建失败，回退到系统 ping")
            self._sock = None
            self._use_native = True

    @staticmethod
    def _checksum(data: bytes) -> int:
        """计算 ICMP 校验和（RFC 1071）"""
        if len(data) % 2:
            data += b'\x00'
        s = 0
        for i in range(0, len(data), 2):
            s += struct.unpack('>H', data[i:i+2])[0]
        s = (s >> 16) + (s & 0xFFFF)
        s += s >> 16
        return (~s) & 0xFFFF

    def _build_packet(self) -> bytes:
        """构造 ICMP Echo Request 包"""
        self._seq = (self._seq + 1) & 0xFFFF
        # Type=8, Code=0, Checksum=0, ID=pid, Seq=seq, Data=timestamp(8B)
        payload = struct.pack('>d', time.monotonic())
        header = struct.pack('>BBHHH', self.ICMP_ECHO_REQUEST, 0, 0, self._pid, self._seq)
        chksum = self._checksum(header + payload)
        header = struct.pack('>BBHHH', self.ICMP_ECHO_REQUEST, 0, chksum, self._pid, self._seq)
        return header + payload

    def ping(self) -> float:
        """
        发送一次 ICMP Echo 并返回延迟（毫秒）。
        返回 -1 表示超时/失败。
        """
        if self._sock and not self._use_native:
            return self._ping_native()
        else:
            return self._ping_subprocess()

    def _ping_native(self) -> float:
        """使用原生 socket 发送 ICMP"""
        packet = self._build_packet()
        send_time = time.monotonic()
        try:
            self._sock.sendto(packet, (self.host, 0))
            while True:
                recv_time = time.monotonic()
                data, _ = self._sock.recvfrom(2048)
                # 解析 ICMP 头（跳过 IP 头 20 字节）
                if len(data) >= 28:
                    icmp_header = data[20:28]
                    _type, _code, _chksum, recv_id, recv_seq = struct.unpack('>BBHHH', icmp_header)
                    if _type == self.ICMP_ECHO_REPLY and recv_id == self._pid:
                        return (recv_time - send_time) * 1000.0
                # 如果不是我们的包，继续等
                if recv_time - send_time > self.timeout:
                    return -1.0
        except socket.timeout:
            return -1.0
        except Exception as e:
            error_stats.record_exception(e, "ICMP 接收异常")
            return -1.0

    def _ping_subprocess(self) -> float:
        """回退方案：调用系统 ping 命令"""
        try:
            if platform.system().lower() == "windows":
                cmd = ["ping", "-n", "1", "-w", str(int(self.timeout * 1000)), self.host]
            else:
                cmd = ["ping", "-c", "1", "-W", str(int(self.timeout)), self.host]
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 errors="replace", timeout=self.timeout + 1)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    if "time=" in line.lower():
                        # 兼容不同语言环境
                        parts = line.lower().split("time=")[1].strip()
                        val = parts.split(" ")[0].split("m")[0]
                        return float(val)
            return -1.0
        except Exception as e:
            error_stats.record_exception(e, "系统 ping 调用失败")
            return -1.0

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except:
                pass
