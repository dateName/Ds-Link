"""
Bufferbloat Tester — 路由器缓冲区膨胀（Bufferbloat）测试工具。

模块划分：
    config    全局常量
    errors    线程安全错误统计
    netinfo   本机网络信息（只读）
    payload   测试数据包构造
    icmp      跨平台 ICMP Ping
    flooder   UDP 发送线程
    monitor   Ping 监测线程
    display   终端输出与报告
    wizard    交互配置（终端 / Tkinter）
    main      测试编排入口
"""

from .config import VERSION

__all__ = ["VERSION"]
