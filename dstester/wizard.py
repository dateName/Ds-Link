"""交互式配置向导（终端 / Tkinter）。"""

from typing import Callable, Optional, Tuple

from .netinfo import get_available_interfaces, get_network_snapshot, validate_local_ip
from .payload import PayloadBuilder


def prompt(text: str, default: str) -> str:
    """带默认值的输入提示"""
    val = input(f"  {text}【{default}】: ").strip()
    return val or default


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
