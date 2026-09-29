"""全局配置常量。"""

VERSION = "3.0"
MAX_RESULTS_HISTORY = 5000          # 内存中保留的最大延迟样本数
DEFAULT_SNDBUF = 64 * 1024          # 默认 64KB（故意设小以暴露 Bufferbloat）
MIN_SNDBUF = 16 * 1024              # 最小 16KB
MAX_SNDBUF = 4 * 1024 * 1024        # 最大 4MB
ERROR_HISTORY_SIZE = 200            # 保留最近 200 条错误
DISPLAY_UPDATE_INTERVAL = 0.5       # 状态行刷新间隔（秒）
SMART_TIP_THRESHOLD = 8             # 多少样本后给出智能提示
