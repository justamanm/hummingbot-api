import re
from datetime import datetime


LOG_CATEGORY_MARKERS = {
    "ordinary": "[分类:普通状态]",
    "buy_tracking": "[分类:买入跟踪]",
    "sell_tracking": "[分类:卖出跟踪]",
    "quote": "[分类:报价查询]",
}
LOG_TIMESTAMP_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")


def log_category(line: str) -> str:
    """兼容分类标记和历史中文日志，不因展示文案升级而丢失旧记录。"""
    for category, marker in LOG_CATEGORY_MARKERS.items():
        if marker in line:
            return category
    if "进入买入跟踪" in line or "买入跟踪中" in line or "正在跟踪买入最低价" in line:
        return "buy_tracking"
    if "进入卖出跟踪" in line or "卖出跟踪中" in line or "正在跟踪卖出最高价" in line:
        return "sell_tracking"
    if "MICRODUCK/USDG报价" in line or "命中分组缓存" in line or "写入分组缓存" in line:
        return "quote"
    if any(text in line for text in ("等待买入", "持仓等待卖出", "正在买入", "正在卖出", "当前交易规则")):
        return "ordinary"
    return "other"


def log_timestamp(line: str) -> datetime | None:
    match = LOG_TIMESTAMP_PATTERN.match(line)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return None


def filter_log_lines(
    lines: list[str],
    *,
    query: str = "",
    category: str = "all",
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> list[dict[str, int | str]]:
    """筛选日志并保留原文件行号，结果始终按最新到最旧排列。"""
    clean_query = query.strip().casefold()
    entries: list[dict[str, int | str]] = []
    for index, line in enumerate(lines):
        if clean_query and clean_query not in line.casefold():
            continue
        if category != "all" and log_category(line) != category:
            continue
        timestamp = log_timestamp(line)
        if start_at is not None and (timestamp is None or timestamp < start_at):
            continue
        if end_at is not None and (timestamp is None or timestamp > end_at):
            continue
        entries.append({"number": index + 1, "text": line})
    entries.reverse()
    return entries
