"""终端输出与测试报告。"""

from datetime import datetime

from .config import VERSION
from .errors import error_stats
from .monitor import PingMonitor


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
