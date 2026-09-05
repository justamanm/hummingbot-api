from datetime import datetime

from utils.bot_logs import filter_log_lines, log_category


def test_log_category_supports_markers_and_historical_messages():
    assert log_category("[分类:普通状态] 等待买入") == "ordinary"
    assert log_category("买入跟踪中，当前价格") == "buy_tracking"
    assert log_category("卖出跟踪中，当前价格") == "sell_tracking"
    assert log_category("MICRODUCK/USDG报价成功") == "quote"
    assert log_category("服务已经启动") == "other"


def test_full_logs_are_newest_first_and_support_combined_filters():
    lines = [
        "2026-09-05 09:00:00,000 - INFO - 等待买入",
        "2026-09-05 10:00:00,000 - INFO - 买入跟踪中，价格 0.02",
        "2026-09-05 11:00:00,000 - INFO - [分类:买入跟踪] 买入跟踪中，价格 0.03",
        "2026-09-05 12:00:00,000 - INFO - MICRODUCK/USDG报价成功",
    ]

    result = filter_log_lines(
        lines,
        query="价格",
        category="buy_tracking",
        start_at=datetime.fromisoformat("2026-09-05T09:30:00"),
        end_at=datetime.fromisoformat("2026-09-05T11:30:00"),
    )

    assert [line["number"] for line in result] == [3, 2]
