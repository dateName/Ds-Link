"""重构行为对比：原版单文件 vs 拆分后的 dstester 包。

原版源码直接从 git 读取（ref 可用 argv[1] 指定，默认 HEAD~... 见下），
避免经过 shell 重定向导致编码损坏。

用法: python parity_check.py [git-ref]
退出码 0 = 全部一致。
"""
import sys
import os
import subprocess
import types
from collections import deque

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

# 拆分前的原始提交，作为行为基线（此脚本用于验证拆分未改变行为）
BASELINE_REF = "84b94a4"
ORIG_REF = sys.argv[1] if len(sys.argv) > 1 else BASELINE_REF

failures = []
checks = 0


def check(name, a, b):
    global checks
    checks += 1
    if a != b:
        failures.append(f"{name}\n    原版: {a!r}\n    新版: {b!r}")


def load_from_git(ref, name):
    """用字节级读取原版源码并 exec，保证与仓库内容完全一致。"""
    raw = subprocess.run(
        ["git", "show", f"{ref}:C65.py"],
        cwd=REPO, capture_output=True, check=True,
    ).stdout
    mod = types.ModuleType(name)
    mod.__file__ = f"<git:{ref}:C65.py>"
    code = compile(raw.decode("utf-8"), mod.__file__, "exec")
    exec(code, mod.__dict__)
    return mod


orig = load_from_git(ORIG_REF, "orig_c65")
print(f"原版来源: git {ORIG_REF}:C65.py ({len(orig.__dict__)} 个全局符号)")

import dstester  # noqa: E402
from dstester import config, errors, netinfo, payload, icmp, flooder, monitor, display, wizard  # noqa: E402
from dstester import main as new_main  # noqa: E402

# ── 1. 常量 ──
check("VERSION", orig.VERSION, config.VERSION)
for const in ("MAX_RESULTS_HISTORY", "DEFAULT_SNDBUF", "MIN_SNDBUF", "MAX_SNDBUF",
              "ERROR_HISTORY_SIZE", "DISPLAY_UPDATE_INTERVAL", "SMART_TIP_THRESHOLD"):
    check(f"常量 {const}", getattr(orig, const), getattr(config, const))

# ── 2. parse_port_range ──
for s in ("53", "1000-2000", " 1-65535 ", "80"):
    def run(fn, arg):
        try:
            return ("ok", fn(arg))
        except Exception as e:
            return ("err", type(e).__name__)
    check(f"parse_port_range({s!r})", run(orig.parse_port_range, s), run(wizard.parse_port_range, s))

# ── 3. build_payload_func 描述 + 生成长度分布 ──
# 注意：dns_query / mixed 的包长是随机的（随机域名长度 4~10），
# 单次比较长度会 flaky，因此比较"长度取值集合"。
def length_profile(fn, n=500):
    return sorted({len(fn()) for _ in range(n)})

for c in ("1", "2", "3", "4", "5", "6", "9"):
    o = orig.build_payload_func(c, 1472)
    n = wizard.build_payload_func(c, 1472)
    check(f"build_payload_func({c!r}) 描述", o[1], n[1])
    check(f"build_payload_func({c!r}) 长度集合", length_profile(o[0]), length_profile(n[0]))

# ── 4. parse_config：合法与非法 ──
valid = {"host": "10.0.0.1", "port": "1-65535", "threads": "32", "duration": "2.5",
         "type_choice": "6", "sndbuf_kb": "128", "ping_interval": "0.5", "local_ip": "10.0.0.2"}
check("parse_config 合法输入", orig.parse_config(dict(valid)), wizard.parse_config(dict(valid)))

bad_cases = [
    {**valid, "port": "0-10"},
    {**valid, "port": "70000"},
    {**valid, "port": "abc"},
    {**valid, "threads": "0"},
    {**valid, "threads": "257"},
    {**valid, "type_choice": "7"},
    {**valid, "sndbuf_kb": "1"},
    {**valid, "ping_interval": "0"},
    {**valid, "duration": "-1"},
    {**valid, "host": "   "},
    {k: v for k, v in valid.items() if k != "host"},
]
for i, case in enumerate(bad_cases):
    def run(fn):
        try:
            return ("ok", fn(dict(case)))
        except Exception as e:
            return ("err", type(e).__name__, str(e))
    check(f"parse_config 非法#{i}", run(orig.parse_config), run(wizard.parse_config))

# ── 5. PayloadBuilder 确定性方法 ──
check("get_zero(64)", orig.PayloadBuilder.get_zero(64), payload.PayloadBuilder.get_zero(64))
check("ntp_request 长度", len(orig.PayloadBuilder.ntp_request()), len(payload.PayloadBuilder.ntp_request()))
check("ssdp_discover", orig.PayloadBuilder.ssdp_discover(), payload.PayloadBuilder.ssdp_discover())
check("get_random 长度", len(orig.PayloadBuilder.get_random(900)), len(payload.PayloadBuilder.get_random(900)))

# ── 6. ErrorStats 行为 ──
import errno as _errno  # noqa: E402
exc_cases = [
    ("EAGAIN", OSError(_errno.EAGAIN, "again")),
    ("ENOBUFS", OSError(_errno.ENOBUFS, "nobufs")),
    ("EACCES", OSError(_errno.EACCES, "denied")),
    ("ENETUNREACH", OSError(_errno.ENETUNREACH, "unreach")),
    ("ECONNREFUSED", OSError(_errno.ECONNREFUSED, "refused")),
    ("EINVAL", OSError(_errno.EINVAL, "inval")),
    ("EMFILE", OSError(_errno.EMFILE, "files")),
    ("plain", ValueError("plain")),
]
o_err, n_err = orig.ErrorStats(), errors.ErrorStats()
for label, exc in exc_cases:
    o_err.record_exception(exc, label)
    n_err.record_exception(exc, label)
check("ErrorStats.total", o_err.total(), n_err.total())
check("ErrorStats.get_summary", o_err.get_summary(), n_err.get_summary())
check("ErrorStats.category counts", o_err._counts, n_err._counts)
check("ErrorStats.should_stop_thread", o_err.should_stop_thread(), n_err.should_stop_thread())
check("ErrorStats.get_recent 行数", len(o_err.get_recent(50).splitlines()), len(n_err.get_recent(50).splitlines()))
check("ErrorStats 空实例 summary", orig.ErrorStats().get_summary(), errors.ErrorStats().get_summary())
check("ErrorStats.record 未知分类归一", orig.ErrorStats().ERROR_CATEGORIES, errors.ErrorStats().ERROR_CATEGORIES)

# ── 7. PingMonitor.get_stats（注入合成数据，不碰网络）──
def stats_of(cls, results):
    m = object.__new__(cls)
    m.results = deque(results, maxlen=5000)
    return m.get_stats()

for label, results in [
    ("空", []),
    ("混合", [10.0, 20.0, -1.0, 30.5, 12.0]),
    ("含丢包", [-1.0, -1.0, -1.0]),
    ("单值", [5.0]),
    ("抖动", [10.0, 50.0, 11.0, 90.0, 12.0, 15.0, 200.0, 13.0, 14.0, 16.0, 17.0]),
]:
    check(f"get_stats {label}", stats_of(orig.PingMonitor, results), stats_of(monitor.PingMonitor, results))

# ── 8. format_status_line（固定时间戳）──
orig.ts = lambda: "00:00:00"
display.ts = lambda: "00:00:00"
for label, st, elapsed in [
    ("正常", {"current": 30.0, "avg": 25.0, "max": 100.0, "jitter": 5.0, "timeout_rate": 0.0, "total": 10}, 12.34),
    ("超时", {"current": -1, "avg": 0, "max": 0, "jitter": 0, "timeout_rate": 100.0, "total": 3}, 5.0),
    ("高延迟", {"current": 350.0, "avg": 300.0, "max": 900.0, "jitter": 88.8, "timeout_rate": 12.5, "total": 80}, 99.9),
]:
    check(f"format_status_line {label}", orig.format_status_line(st, elapsed), display.format_status_line(st, elapsed))

# ── 9. 符号归属 ──
expected = {
    "_first_ipv4": netinfo, "get_local_ip": netinfo, "get_available_interfaces": netinfo,
    "validate_local_ip": netinfo, "get_default_gateway": netinfo, "get_dns_servers": netinfo,
    "get_network_snapshot": netinfo, "print_network_info": netinfo,
    "ErrorStats": errors, "PayloadBuilder": payload, "IcmpPing": icmp,
    "UdpFlooder": flooder, "PingMonitor": monitor,
    "ts": display, "format_status_line": display, "print_banner": display,
    "print_final_report": display,
    "prompt": wizard, "parse_port_range": wizard, "build_payload_func": wizard,
    "parse_config": wizard, "terminal_wizard": wizard,
    "terminal_wizard_from_values": wizard, "tkinter_wizard": wizard, "get_test_config": wizard,
    "stop_test": new_main, "main": new_main,
}
for sym, mod in expected.items():
    check(f"符号 {sym} -> {mod.__name__}", hasattr(mod, sym), True)
check("error_stats 单例在 errors.py", hasattr(errors, "error_stats"), True)

# 原版所有顶层公开符号都应有归属
# 排除：常量（已在第 1 节核对）、导入的模块名（不属于本项目符号）
NON_SYMBOLS = {
    # 常量
    "VERSION", "MAX_RESULTS_HISTORY", "DEFAULT_SNDBUF", "MIN_SNDBUF", "MAX_SNDBUF",
    "ERROR_HISTORY_SIZE", "DISPLAY_UPDATE_INTERVAL", "SMART_TIP_THRESHOLD",
    # 原版顶层 import 的模块 / typing 名字
    "socket", "threading", "time", "os", "subprocess", "sys", "random", "struct",
    "platform", "errno", "re", "json", "deque", "datetime", "Optional", "Callable", "Tuple",
}
orig_public = {n for n in dir(orig) if not n.startswith("__")}
missing = orig_public - set(expected) - {"error_stats"} - NON_SYMBOLS
check("原版公开符号是否全部覆盖", sorted(missing), [])

print(f"共 {checks} 项检查")
if failures:
    print(f"\n[FAIL] {len(failures)} 项不一致:\n")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\n[PASS] 全部一致：重构未改变行为")
