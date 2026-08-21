#!/usr/bin/env python3
"""
╔════════════════════════════════════════════════════════════════════╗
║   Bufferbloat Tester v3.0 — 路由器缓冲区膨胀测试工具              ║
║                                                                    ║
║   改进点:                                                          ║
║   ✅ 完整异常处理体系（告别静默失败）                              ║
║   ✅ Socket 缓冲区溢出智能重试                                     ║
║   ✅ 跨平台 ICMP Ping（纯 Python 实现，无需系统 ping）             ║
║   ✅ 随机端口 / 端口范围支持                                       ║
║   ✅ 可配置 Socket 缓冲区大小                                      ║
║   ✅ 结构化时间戳输出 + 状态图标                                   ║
║   ✅ 线程安全错误统计                                              ║
║   ✅ Payload 构造优化（减少 GC 压力）                              ║
║   ✅ 指数退避 + 自适应速率控制                                     ║
╚════════════════════════════════════════════════════════════════════╝
"""

import socket
import threading
import time
import os
import subprocess
import sys
import random
import struct
import platform
import errno
import re
import json
from collections import deque
from datetime import datetime
from typing import Optional, Callable, Tuple

# ═════════════════════════════════════════════════════════════════════
#  全局配置
# ═════════════════════════════════════════════════════════════════════
VERSION = "3.0"
MAX_RESULTS_HISTORY = 5000          # 内存中保留的最大延迟样本数
DEFAULT_SNDBUF = 64 * 1024          # 默认 64KB（故意设小以暴露 Bufferbloat）
MIN_SNDBUF = 16 * 1024              # 最小 16KB
MAX_SNDBUF = 4 * 1024 * 1024        # 最大 4MB
ERROR_HISTORY_SIZE = 200            # 保留最近 200 条错误
DISPLAY_UPDATE_INTERVAL = 0.5       # 状态行刷新间隔（秒）
SMART_TIP_THRESHOLD = 8             # 多少样本后给出智能提示


# ═════════════════════════════════════════════════════════════════════
#  本机网络信息（只读）
# ═════════════════════════════════════════════════════════════════════
def _first_ipv4(value: str) -> str:
    """从命令输出中提取第一个 IPv4 地址。"""
    match = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", value)
    return match.group(0) if match else "未检测到"


def get_local_ip() -> str:
    """获取当前出站网卡 IPv4，不发送应用层数据。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return _first_ipv4(socket.gethostbyname(socket.gethostname()))
        except OSError:
            return "未检测到"
    finally:
        sock.close()


def get_available_interfaces() -> list:
    """读取可用 IPv4 网卡，返回 [{name, ip}]，失败时返回空列表。"""
    interfaces = []
    system = platform.system().lower()
    if system == "windows":
        command = ["powershell", "-NoProfile", "-Command",
                   "Get-NetIPAddress -AddressFamily IPv4 "
                   "| Where-Object {$_.IPAddress -notlike '127.*'} "
                   "| Select-Object InterfaceAlias,IPAddress "
                   "| ConvertTo-Json -Compress"]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=3, check=False)
            data = json.loads(result.stdout) if result.stdout.strip() else []
            if isinstance(data, dict):
                data = [data]
            for item in data:
                ip = item.get("IPAddress")
                name = item.get("InterfaceAlias", "未知网卡")
                if ip and ip != "0.0.0.0":
                    interfaces.append({"name": name, "ip": ip})
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            pass
        if not interfaces:
            try:
                result = subprocess.run(["ipconfig", "/all"], capture_output=True,
                                        text=True, timeout=3, check=False)
                interface_name = "Windows 网卡"
                for line in result.stdout.splitlines():
                    stripped = line.strip()
                    if line and not line[0].isspace() and stripped.endswith(":"):
                        interface_name = stripped[:-1]
                    if "IPv4" in line or "IPv4 地址" in line:
                        ip = _first_ipv4(line)
                        if ip != "未检测到" and not ip.startswith("127."):
                            interfaces.append({"name": interface_name, "ip": ip})
            except (OSError, subprocess.SubprocessError):
                pass
    else:
        try:
            result = subprocess.run(["ip", "-o", "-4", "addr", "show"],
                                    capture_output=True, text=True, timeout=3,
                                    check=False)
            for line in result.stdout.splitlines():
                match = re.search(r"^\d+:\s+(\S+).*?inet\s+(\d+\.\d+\.\d+\.\d+)/", line)
                if match and not match.group(2).startswith("127."):
                    interfaces.append({"name": match.group(1), "ip": match.group(2)})
        except (OSError, subprocess.SubprocessError):
            pass
    return interfaces


def validate_local_ip(local_ip: str) -> bool:
    """确认 IPv4 仍可作为本机 socket 源地址使用。"""
    if not local_ip:
        return True
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((local_ip, 0))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def get_default_gateway() -> str:
    """读取默认网关，兼容 Windows、Linux 和 Termux。"""
    system = platform.system().lower()
    commands = [["ip", "route", "show", "default"]]
    if system == "windows":
        commands = [["route", "print", "-4"]]
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=3, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if command[0] == "ip" and result.returncode == 0:
            match = re.search(r"default via\s+(\S+)", result.stdout)
            if match:
                return match.group(1)
        if command[0] == "route":
            match = re.search(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\S+)", result.stdout,
                              re.MULTILINE)
            if match:
                return _first_ipv4(match.group(1))
    return "未检测到"


def get_dns_servers() -> list:
    """读取 DNS 配置，不修改系统设置。"""
    servers = []
    system = platform.system().lower()
    if system == "windows":
        try:
            command = ["powershell", "-NoProfile", "-Command",
                       "Get-DnsClientServerAddress -AddressFamily IPv4 "
                       "| Select-Object -ExpandProperty ServerAddresses"]
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=3, check=False)
            for line in result.stdout.splitlines():
                address = _first_ipv4(line)
                if address != "未检测到" and address not in servers:
                    servers.append(address)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            with open("/etc/resolv.conf", encoding="utf-8") as resolv:
                for line in resolv:
                    match = re.match(r"\s*nameserver\s+(\S+)", line)
                    if match and match.group(1) not in servers:
                        servers.append(match.group(1))
        except OSError:
            pass
    return servers or ["未检测到"]


def get_network_snapshot() -> dict:
    """返回本机网络环境快照。"""
    return {
        "platform": f"{platform.system()} {platform.release()}".strip(),
        "ip": get_local_ip(),
        "gateway": get_default_gateway(),
        "dns": get_dns_servers(),
        "hostname": socket.gethostname(),
        "interfaces": get_available_interfaces(),
    }


def print_network_info() -> None:
    """显示当前网络信息；该操作为只读。"""
    info = get_network_snapshot()
    print("\n  ┌─ 当前网络环境（只读）──────────────────────")
    print(f"     系统:       {info['platform']}")
    print(f"     主机名:     {info['hostname']}")
    print(f"     IPv4 地址:  {info['ip']}")
    print(f"     默认网关:   {info['gateway']}")
    print(f"     DNS:        {', '.join(info['dns'])}")
    print("     可用网卡:")
    for interface in info["interfaces"] or [{"name": "自动选择", "ip": info["ip"]}]:
        print(f"       - {interface['name']}: {interface['ip']}")
    print("     说明:       本程序不会修改上述网络配置。")
    print("  └──────────────────────────────────────────\n")


# ═════════════════════════════════════════════════════════════════════
#  错误统计模块（线程安全）
# ═════════════════════════════════════════════════════════════════════
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


# ═════════════════════════════════════════════════════════════════════
#  Payload 构造模块（优化版）
# ═════════════════════════════════════════════════════════════════════
class PayloadBuilder:
    """
    高性能 Payload 构造器。
    - 预生成常用尺寸的随机数据，避免频繁调用 os.urandom()
    - DNS/NTP/SSDP 按需构造
    """

    _cache: dict = {}
    _cache_lock = threading.Lock()

    @classmethod
    def get_random(cls, size: int) -> bytes:
        """获取指定大小的随机 payload（带缓存）"""
        with cls._cache_lock:
            if size not in cls._cache:
                # 预生成并缓存（最多缓存 10 种尺寸）
                if len(cls._cache) > 10:
                    cls._cache.clear()
                cls._cache[size] = os.urandom(size)
            return cls._cache[size]

    @classmethod
    def get_zero(cls, size: int) -> bytes:
        """获取全零 payload"""
        return b'\x00' * size

    @classmethod
    def dns_query(cls) -> bytes:
        """构造随机域名 DNS 查询包 (约 40~60 字节)"""
        tid = random.randint(0, 65535)
        domain = ''.join(random.choices(
            'abcdefghijklmnopqrstuvwxyz', k=random.randint(4, 10)
        )) + '.com'
        qname = b''
        for part in domain.split('.'):
            qname += bytes([len(part)]) + part.encode()
        qname += b'\x00'
        return struct.pack('>HHHHHH', tid, 0x0100, 1, 0, 0, 0) + qname + struct.pack('>HH', 1, 1)

    @classmethod
    def ntp_request(cls) -> bytes:
        """NTP 客户端请求包 (48 字节)"""
        return b'\x1b' + b'\x00' * 47

    @classmethod
    def ssdp_discover(cls) -> bytes:
        """SSDP M-SEARCH 发现包 (约 130 字节)"""
        return (b"M-SEARCH * HTTP/1.1\r\n"
                b"HOST:239.255.255.250:1900\r\n"
                b"MAN:\"ssdp:discover\"\r\n"
                b"ST:ssdp:all\r\n\r\n")

    @classmethod
    def mixed(cls, size: int) -> bytes:
        """混合模式：40% 随机大包 + 30% DNS + 30% NTP"""
        r = random.random()
        if r < 0.4:
            return cls.get_random(size)
        elif r < 0.7:
            return cls.dns_query()
        else:
            return cls.ntp_request()


# ═════════════════════════════════════════════════════════════════════
#  跨平台 ICMP Ping 实现
# ═════════════════════════════════════════════════════════════════════
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
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout + 1)
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


# ═════════════════════════════════════════════════════════════════════
#  UDP 发送线程（改进版）
# ═════════════════════════════════════════════════════════════════════
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


# ═════════════════════════════════════════════════════════════════════
#  Ping 监测线程（改进版）
# ═════════════════════════════════════════════════════════════════════
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


# ═════════════════════════════════════════════════════════════════════
#  显示模块
# ═════════════════════════════════════════════════════════════════════
def ts() -> str:
    """返回当前时间戳字符串"""
    return datetime.now().strftime('%H:%M:%S')


def format_status_line(stats: dict, elapsed: float) -> str:
    """格式化单行状态输出"""
    if stats['current'] > 0:
        icon = "🟢" if stats['current'] < 50 else "🟡" if stats['current'] < 200 else "🔴"
        curr_str = f"{stats['current']:.1f}ms"
    else:
        icon = "🔴"
        curr_str = "超时"

    return (
        f"\r[{ts()}] ⏱ {elapsed:6.1f}s | "
        f"{icon} 当前:{curr_str:>8s} | "
        f"平均:{stats['avg']:7.1f}ms | "
        f"最大:{stats['max']:7.1f}ms | "
        f"抖动:{stats['jitter']:6.1f}ms | "
        f"丢包:{stats['timeout_rate']:5.1f}% | "
        f"样本:{stats['total']}"
    )


def print_banner():
    """打印启动横幅"""
    bar = "═" * 58
    print(f"\n{bar}")
    print(f"   ⚡ Bufferbloat Tester v{VERSION} — 路由器缓冲区膨胀测试")
    print(f"{bar}")
    print("   目标：占满上行带宽 → 触发路由器队列积压 → 观测延迟飙升")
    print("   用途：测试路由器 Bufferbloat / QoS / AQM 效果\n")


def print_final_report(ping_mon: PingMonitor, flooders: list, elapsed: float):
    """打印最终测试报告"""
    bar = "─" * 58
    print(f"\n\n{bar}")
    print(f"  📋 测试报告  [{ts()}]")
    print(bar)

    stats = ping_mon.get_stats()
    if stats['total'] > 0:
        print(f"  ⏱  总时长:       {elapsed:.1f} 秒")
        print(f"  📊 Ping 样本:    {stats['total']} 次")
        print(f"  ✅ 有效回复:     {stats['valid_count']} 次")
        print(f"  ❌ 超时/丢包:    {stats['timeout_count']} 次 ({stats['timeout_rate']:.1f}%)")
        if stats['valid_count'] > 0:
            print(f"  📈 最小延迟:     {stats['min']:.1f} ms")
            print(f"  📈 平均延迟:     {stats['avg']:.1f} ms")
            print(f"  📈 最大延迟:     {stats['max']:.1f} ms")
            print(f"  📈 平均抖动:     {stats['jitter']:.1f} ms")

            # Bufferbloat 评级
            if stats['max'] > 1000:
                rating = "🔴 严重 Bufferbloat（延迟 >1s）"
            elif stats['max'] > 500:
                rating = "🟠 明显 Bufferbloat（延迟 500ms~1s）"
            elif stats['max'] > 200:
                rating = "🟡 中等 Bufferbloat（延迟 200~500ms）"
            elif stats['max'] > 100:
                rating = "🟢 轻微 Bufferbloat（延迟 100~200ms）"
            else:
                rating = "✅ 无明显 Bufferbloat"
            print(f"  🏷  评级:         {rating}")

    # 发送统计
    print(f"\n  📤 发送统计:")
    total_pkts = sum(f.packets_sent for f in flooders)
    total_bytes = sum(f.bytes_sent for f in flooders)
    total_errs = sum(f.send_errors for f in flooders)
    alive = sum(1 for f in flooders if f.is_alive())
    print(f"     总包数:       {total_pkts}")
    print(f"     总流量:       {total_bytes / (1024*1024):.1f} MB")
    print(f"     发送错误:     {total_errs}")
    print(f"     存活线程:     {alive}/{len(flooders)}")

    # 错误摘要
    print(f"\n  ⚠️  错误摘要:")
    print(error_stats.get_summary())
    if error_stats.total() > 0:
        print(f"\n  📝 最近错误:")
        print(error_stats.get_recent(5))

    print(f"\n{bar}")
    print("  🛑 测试结束，流量已停止。")
    print(bar)


# ═════════════════════════════════════════════════════════════════════
#  交互输入模块
# ═════════════════════════════════════════════════════════════════════
def prompt(text: str, default: str) -> str:
    """带默认值的输入提示"""
    val = input(f"  {text}【{default}】: ").strip()
    return val or default


def stop_test(stop_event: threading.Event, ping_mon: PingMonitor, flooders: list) -> None:
    """停止测试并回收线程；不会撤销或修改系统网络配置。"""
    stop_event.set()
    for flooder in flooders:
        flooder.join(timeout=2)
    ping_mon.stop()
    ping_mon.join(timeout=2)


def parse_port_range(s: str) -> Tuple[int, int]:
    """解析端口范围字符串，如 '53' → (53,53), '1000-2000' → (1000,2000)"""
    s = s.strip()
    if '-' in s:
        parts = s.split('-')
        return int(parts[0]), int(parts[1])
    else:
        p = int(s)
        return p, p


def build_payload_func(choice: str, size: int) -> Tuple[Callable[[], bytes], str]:
    """根据选择构建 payload 生成函数"""
    if choice == "1":
        return lambda: PayloadBuilder.get_random(size), "随机数据(大包)"
    elif choice == "2":
        return lambda: PayloadBuilder.get_zero(size), "全零数据(大包)"
    elif choice == "3":
        return PayloadBuilder.dns_query, "DNS 查询(短包)"
    elif choice == "4":
        return PayloadBuilder.ntp_request, "NTP 请求(短包)"
    elif choice == "5":
        return PayloadBuilder.ssdp_discover, "SSDP 发现(短包)"
    elif choice == "6":
        return lambda: PayloadBuilder.mixed(size), "混合模式"
    else:
        return lambda: PayloadBuilder.get_random(size), "随机数据(大包)"


def parse_config(values: dict) -> Optional[dict]:
    """校验向导输入并转换为测试参数。"""
    try:
        host = values["host"].strip()
        if not host:
            raise ValueError("目标地址不能为空")
        port_min, port_max = parse_port_range(values["port"])
        if not 1 <= port_min <= port_max <= 65535:
            raise ValueError("端口必须在 1~65535 范围内")
        threads = int(values["threads"])
        duration = float(values["duration"])
        sndbuf_kb = int(values["sndbuf_kb"])
        ping_interval = float(values["ping_interval"])
        if threads < 1 or threads > 256:
            raise ValueError("线程数必须在 1~256 范围内")
        if duration < 0 or sndbuf_kb < 16 or ping_interval <= 0:
            raise ValueError("时长、缓冲区或 Ping 间隔不合法")
        if values["type_choice"] not in {"1", "2", "3", "4", "5", "6"}:
            raise ValueError("数据包类型必须是 1~6")
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(str(error)) from error
    return {
        "host": host,
        "port_min": port_min,
        "port_max": port_max,
        "threads": threads,
        "duration": duration,
        "type_choice": values["type_choice"],
        "sndbuf_size": sndbuf_kb * 1024,
        "ping_interval": ping_interval,
        "local_ip": values.get("local_ip", ""),
    }


def terminal_wizard(local_ip: str = "") -> Optional[dict]:
    """无图形环境时使用的分步向导，输入 b 返回上一步。"""
    values = {
        "host": "192.168.1.1", "port": "53", "threads": "16",
        "duration": "0", "type_choice": "1", "sndbuf_kb": "64",
        "ping_interval": "0.3",
        "local_ip": local_ip,
    }
    return terminal_wizard_from_values(values)


def terminal_wizard_from_values(values: dict, start_step: int = 0) -> Optional[dict]:
    """从指定步骤继续终端向导，保留用户已填写的值。"""
    steps = [
        ("基本配置", [("host", "目标 IP 地址"), ("port", "端口（单端口如 53，或范围如 1-65535）"),
                       ("threads", "线程数（建议 8~64）"), ("duration", "测试时长/秒（0=无限，Ctrl+C 停止）")]),
        ("数据包类型", [("type_choice", "请选择")]),
        ("高级配置", [("sndbuf_kb", "Socket 发送缓冲区 KB（64=暴露 Bufferbloat, 1024=隐藏延迟）"),
                       ("ping_interval", "Ping 间隔秒（默认 0.3）")]),
    ]
    step = start_step
    while step < len(steps):
        title, fields = steps[step]
        if step == 0:
            print("\n  ┌─ 基本配置 ────────────────────────────────")
        elif step == 1:
            print("\n  ┌─ 数据包类型 ──────────────────────────────")
            print("    1. 随机数据（不可压缩，效果最好）")
            print("    2. 全零数据（可被压缩优化，效果差）")
            print("    3. DNS 查询（短包，压路由器 CPU/会话表）")
            print("    4. NTP 请求（短包）")
            print("    5. SSDP 发现（短包，多播）")
            print("    6. 混合模式（随机大包 + DNS + NTP）")
        else:
            print("\n  ┌─ 高级配置 ────────────────────────────────")
        print("  （输入 b 返回上一步）")
        try:
            for key, label in fields:
                value = input(f"  {label}【{values[key]}】: ").strip()
                if value.lower() == "b":
                    step = max(0, step - 1)
                    break
                if value:
                    values[key] = value
            else:
                step += 1
        except (KeyboardInterrupt, EOFError):
            return None
    try:
        config = parse_config(values)
    except ValueError as error:
        print(f"  输入有误：{error}")
        return terminal_wizard_from_values(values, start_step=0)
    port_desc = f"固定端口({config['port_min']})" if config["port_min"] == config["port_max"] else f"随机端口({config['port_min']}~{config['port_max']})"
    _, data_desc = build_payload_func(config["type_choice"], 1472)
    print("\n  ┌─ 配置确认 ────────────────────────────────")
    print(f"     目标:     {config['host']}")
    print(f"     端口:     {port_desc}")
    print(f"     线程:     {config['threads']}")
    print(f"     数据:     {data_desc}")
    print(f"     SO_SNDBUF:{config['sndbuf_size'] // 1024}KB")
    print(f"     Ping间隔: {config['ping_interval']}s")
    duration_desc = "无限 (Ctrl+C 停止)" if config["duration"] == 0 else f"{config['duration']:.2f} 秒"
    print(f"     时长:     {duration_desc}")
    print(f"{'─' * 58}")
    confirm = input("  确认开始? [Y/n]: ").strip().lower() or "y"
    if confirm == "b":
        return terminal_wizard_from_values(values, start_step=2)
    return config if confirm in {"y", "yes"} else None


def tkinter_wizard(local_ip: str = "") -> Optional[dict]:
    """Tkinter 分步向导；导入或创建窗口失败时返回 None。"""
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        return None

    values = {
        "host": "192.168.1.1", "port": "53", "threads": "16",
        "duration": "0", "type_choice": "1", "sndbuf_kb": "64",
        "ping_interval": "0.3",
        "local_ip": local_ip,
    }
    try:
        root = tk.Tk()
    except tk.TclError:
        return None
    root.title("Bufferbloat Tester - 测试配置")
    root.geometry("700x620")
    root.minsize(620, 560)
    root.configure(bg="#f4f6f8")
    result = {"config": None}
    step = {"index": 0}
    fields = [
        [("host", "目标 IP 地址", "192.168.1.1"), ("port", "端口或端口范围", "53"),
         ("threads", "线程数", "16"), ("duration", "测试时长（秒，0=无限）", "0")],
        [("type_choice", "数据包类型（1~6）", "1")],
        [("sndbuf_kb", "发送缓冲区 KB（最小 16）", "64"), ("ping_interval", "Ping 间隔（秒）", "0.3")],
    ]
    packet_types = {
        "1": "随机数据（大包）：不可压缩，适合观察带宽占用",
        "2": "全零数据（大包）：可压缩，实际压力可能较低",
        "3": "DNS 查询（短包）：观察路由器 DNS/会话处理能力",
        "4": "NTP 请求（短包）：模拟时间同步请求",
        "5": "SSDP 发现（短包）：模拟局域网设备发现请求",
        "6": "混合模式：随机大包 + DNS + NTP",
    }
    titles = ["基本配置", "数据包类型", "高级配置", "确认"]
    body = ttk.Frame(root, padding=22)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text="Bufferbloat Tester", font=("Segoe UI", 18, "bold")).pack(anchor="w")
    network = get_network_snapshot()
    network_text = ("当前网络环境（只读）\n"
                    f"系统：{network['platform']}\n"
                    f"主机名：{network['hostname']}\n"
                    f"IPv4 地址：{network['ip']}\n"
                    f"默认网关：{network['gateway']}\n"
                    f"DNS：\n  " + "\n  ".join(network["dns"]))
    ttk.Label(body, text=network_text, justify="left", wraplength=560).pack(anchor="w", pady=(4, 18))
    title_var = tk.StringVar()
    ttk.Label(body, textvariable=title_var, font=("Segoe UI", 12, "bold")).pack(anchor="w")
    form = ttk.Frame(body)
    form.pack(fill="both", expand=True, pady=12)
    error_var = tk.StringVar()
    ttk.Label(body, textvariable=error_var, foreground="#b42318").pack(anchor="w")
    buttons = ttk.Frame(body, height=42)
    buttons.pack(fill="x", pady=(12, 0))
    buttons.pack_propagate(False)

    def finish(config):
        result["config"] = config
        root.destroy()

    def render():
        for child in form.winfo_children():
            child.destroy()
        index = step["index"]
        title_var.set(f"步骤 {index + 1}/4：{titles[index]}")
        error_var.set("")
        if index < 3:
            if index == 1:
                ttk.Label(form, text="选择数据包类型").grid(row=0, column=0, sticky="w", pady=8)
                type_var = tk.StringVar(value=values["type_choice"])
                selector = ttk.Combobox(
                    form, textvariable=type_var, state="readonly", width=54,
                    values=[f"{key}. {description}" for key, description in packet_types.items()])
                selector.current(max(0, int(values["type_choice"]) - 1))
                selector.grid(row=0, column=1, sticky="w", padx=16, pady=8)

                def update_packet_type(event=None):
                    values["type_choice"] = str(selector.current() + 1)

                selector.bind("<<ComboboxSelected>>", update_packet_type)
                ttk.Label(form, text="数据包说明", foreground="#475467").grid(row=1, column=0, sticky="nw", pady=8)
                ttk.Label(form, text="\n".join(packet_types.values()), justify="left", wraplength=430).grid(
                    row=1, column=1, sticky="w", padx=16, pady=8)
            else:
                for row, (key, label, default) in enumerate(fields[index]):
                    ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=8)
                    entry = ttk.Entry(form, width=38)
                    entry.insert(0, values[key])
                    entry.grid(row=row, column=1, sticky="w", padx=16, pady=8)
                    entry.bind("<KeyRelease>", lambda event, k=key, e=entry: values.__setitem__(k, e.get().strip()))
                    entry.bind("<Return>", lambda event: next_step())
            back_button.state(["disabled"] if index == 0 else ["!disabled"])
            next_button.configure(text="下一步")
        else:
            try:
                config = parse_config(values)
                port_desc = (f"固定端口({config['port_min']})"
                             if config["port_min"] == config["port_max"]
                             else f"随机端口({config['port_min']}~{config['port_max']})")
                _, data_desc = build_payload_func(config["type_choice"], 1472)
                duration_desc = ("无限 (Ctrl+C 停止)" if config["duration"] == 0
                                 else f"{config['duration']:.2f} 秒")
                summary = (f"目标：{config['host']}\n"
                           f"端口：{port_desc}\n"
                           f"线程：{config['threads']}\n"
                           f"数据：{data_desc}\n"
                           f"SO_SNDBUF：{config['sndbuf_size'] // 1024} KB\n"
                           f"Ping 间隔：{config['ping_interval']:.2f} 秒\n"
                           f"本地网卡：{config.get('local_ip') or '自动选择'}\n"
                           f"时长：{duration_desc}")
                ttk.Label(form, text=summary, justify="left").pack(anchor="w", pady=18)
            except ValueError as error:
                error_var.set(str(error))
            back_button.state(["!disabled"])
            next_button.configure(text="开始测试")

    def next_step():
        if step["index"] < 3:
            try:
                if step["index"] == 0:
                    parse_config({**values, "type_choice": "1", "sndbuf_kb": "64", "ping_interval": "0.3"})
                step["index"] += 1
                render()
            except ValueError as error:
                error_var.set(str(error))
        else:
            try:
                finish(parse_config(values))
            except ValueError as error:
                error_var.set(str(error))

    def back_step():
        if step["index"] > 0:
            step["index"] -= 1
            render()

    def handle_back_key(event):
        if event.widget.winfo_class() in {"Entry", "TEntry", "Combobox"}:
            return
        back_step()
        return "break"

    back_button = ttk.Button(buttons, text="上一步", command=back_step)
    back_button.pack(side="left")
    ttk.Button(buttons, text="取消", command=root.destroy).pack(side="right", padx=(8, 0))
    next_button = ttk.Button(buttons, text="下一步 ▶", command=next_step)
    next_button.pack(side="right")
    root.bind("<KeyPress-b>", handle_back_key)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    render()
    root.mainloop()
    return result["config"]


def get_test_config() -> Optional[dict]:
    """先询问是否使用图形界面，再选择对应的配置向导。"""
    try:
        choice = input("\n  是否使用 Tkinter 图形配置界面？[Y/n]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return None
    interfaces = get_available_interfaces()
    selected_ip = ""
    if interfaces:
        print("\n  可用网卡：")
        for index, interface in enumerate(interfaces, 1):
            print(f"    {index}. {interface['name']} - {interface['ip']}")
        selection = input("  选择网卡编号（直接回车=自动选择）: ").strip()
        if selection.isdigit() and 1 <= int(selection) <= len(interfaces):
            selected_ip = interfaces[int(selection) - 1]["ip"]
            if not validate_local_ip(selected_ip):
                print(f"  网卡地址 {selected_ip} 当前不可用，已改为自动选择。")
                selected_ip = ""
    if choice in {"", "y", "yes"}:
        config = tkinter_wizard(selected_ip)
        if config is not None:
            return config
        print("  当前环境无法打开图形界面，已切换到命令行向导。")
    return terminal_wizard(selected_ip)


# ═════════════════════════════════════════════════════════════════════
#  主程序
# ═════════════════════════════════════════════════════════════════════
def main():
    print_banner()
    print_network_info()
    config = get_test_config()
    if config is None:
        print("  已取消；没有修改系统网络配置。")
        return

    host = config["host"]
    port_min = config["port_min"]
    port_max = config["port_max"]
    use_random_port = (port_min != port_max)
    threads = config["threads"]
    duration = config["duration"]
    type_choice = config["type_choice"]
    sndbuf_size = config["sndbuf_size"]
    ping_interval = config["ping_interval"]
    local_ip = config.get("local_ip", "")

    # ── 构建 payload 函数 ──
    pkt_size = 1472  # UDP 大包大小（MTU 1500 - IP头20 - UDP头8）
    payload_func, desc = build_payload_func(type_choice, pkt_size)

    # ── 端口策略 ──
    if use_random_port:
        port_desc = f"随机端口({port_min}~{port_max})"
        def port_func():
            return random.randint(port_min, port_max)
    else:
        port_desc = f"固定端口({port_min})"
        def port_func():
            return port_min

    # ── 确认信息 ──
    print(f"\n  ┌─ 配置确认 ────────────────────────────────")
    print(f"     目标:     {host}")
    print(f"     端口:     {port_desc}")
    print(f"     线程:     {threads}")
    print(f"     数据:     {desc}")
    print(f"     SO_SNDBUF:{sndbuf_size // 1024}KB")
    print(f"     Ping间隔: {ping_interval}s")
    print(f"     本地网卡: {local_ip or '自动选择'}")
    duration_desc = "无限 (Ctrl+C 停止)" if duration == 0 else f"{duration:.2f} 秒"
    print(f"     时长:     {duration_desc}")
    print(f"{'─' * 58}")

    print(f"\n  🔥 开始测试... 按 Ctrl+C 随时停止\n")

    # ── 启动 ──
    stop_event = threading.Event()
    ping_mon = PingMonitor(host, interval=ping_interval, timeout=1.0)
    ping_mon.start()

    flooders = []
    for i in range(threads):
        f = UdpFlooder(
            host=host,
            port_func=port_func,
            payload_func=payload_func,
            stop_event=stop_event,
            sndbuf_size=sndbuf_size,
            thread_id=i,
            local_ip=local_ip,
        )
        f.start()
        flooders.append(f)

    start_time = time.monotonic()
    last_display = 0.0
    smart_tip_shown = False

    try:
        while True:
            now = time.monotonic()
            elapsed = now - start_time

            # 检查时长
            if duration > 0 and elapsed >= duration:
                print(f"\n\n  ⏰ 设定时间到 ({duration:.2f}s)，正在停止...")
                break

            # 定期刷新状态行
            if now - last_display >= DISPLAY_UPDATE_INTERVAL:
                last_display = now
                stats = ping_mon.get_stats()
                sys.stdout.write(format_status_line(stats, elapsed))
                sys.stdout.flush()

                # 智能提示：延迟持续很低
                if not smart_tip_shown:
                    valid = [l for l in ping_mon.results if l > 0]
                    if len(valid) >= SMART_TIP_THRESHOLD and all(l < 20 for l in valid):
                        print("\n  💡 延迟一直很低，建议:")
                        print("     • 增加线程数（当前 {0}，可尝试 2~4 倍）".format(threads))
                        print("     • 改用短包模式（DNS/NTP）压路由器 CPU")
                        print("     • 减小 SO_SNDBUF（当前 {0}KB → 尝试 16KB）".format(sndbuf_size // 1024))
                        print("     • 确认目标 IP 确实经过该路由器")
                        smart_tip_shown = True

            time.sleep(0.1)

    except KeyboardInterrupt:
        print(f"\n\n  ⏹️  用户中断，已撤销当前测试步骤 (运行 {time.monotonic() - start_time:.1f}s)...")
    finally:
        stop_test(stop_event, ping_mon, flooders)

        elapsed = time.monotonic() - start_time
        print_final_report(ping_mon, flooders, elapsed)


if __name__ == "__main__":
    main()
