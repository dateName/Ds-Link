"""测试数据包构造。"""

import os
import random
import struct
import threading


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
