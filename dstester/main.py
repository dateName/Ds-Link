"""测试编排入口。"""

import random
import sys
import threading
import time

from .config import DISPLAY_UPDATE_INTERVAL, SMART_TIP_THRESHOLD
from .display import format_status_line, print_banner, print_final_report
from .flooder import UdpFlooder
from .monitor import PingMonitor
from .netinfo import print_network_info
from .wizard import build_payload_func, get_test_config


def _setup_console() -> None:
    """保证终端输出不因编码问题崩溃。

    Windows 控制台在中文等区域默认使用 GBK 等非 UTF-8 代码页，无法编码
    banner 与状态行里的 ⚡🟢 等字符，会导致启动即 raise UnicodeEncodeError。
    这里把编码错误策略降级为 replace：可编码的正常显示，无法编码的显示为
    ?，程序不再中断（在 UTF-8 终端下则完全无影响）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def stop_test(stop_event: threading.Event, ping_mon: PingMonitor, flooders: list) -> None:
    """停止测试并回收线程；不会撤销或修改系统网络配置。"""
    stop_event.set()
    for flooder in flooders:
        flooder.join(timeout=2)
    ping_mon.stop()
    ping_mon.join(timeout=2)


def main():
    _setup_console()
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
