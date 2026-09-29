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

入口脚本。实现拆分在 dstester/ 包中，各模块职责见 dstester/__init__.py。

用法:
    python C65.py
"""

import os
import sys

# 标准 Python 会把脚本所在目录放入 sys.path[0]，但部分嵌入式/改造过的
# 运行时不会，导致 `import dstester` 失败。这里显式补上（正常环境下是空操作）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dstester.main import main

if __name__ == "__main__":
    main()
