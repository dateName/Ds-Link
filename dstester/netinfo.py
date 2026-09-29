"""本机网络信息探测（只读，不修改任何系统网络配置）。"""

import json
import platform
import re
import socket
import subprocess


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
