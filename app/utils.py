"""通用小工具（无界面依赖）：时间/时长的人话格式化。

为什么单独一个模块：这两个函数此前住在 app/ui/card_view.py（卡片墙
视图层），detail_panel 为了用它们不得不从绘制模块导入工具，依赖方向
别扭。下沉到这里后，任何模块都可以放心导入，不沾 Qt/界面。
"""
from datetime import datetime


def rel_time(iso) -> str:
    """ISO 时间 → 人话相对时间（"刚刚"/"3小时前"/"5天前"/"2个月前"/"1年前"）。"""
    try:
        dt = datetime.fromisoformat(iso)
        secs = (datetime.now() - dt).total_seconds()
        if secs < 0:
            return "刚刚"
        if secs < 3600:
            return "刚刚"
        if secs < 86400:
            return f"{int(secs // 3600)}小时前"
        days = int(secs // 86400)
        if days < 30:
            return f"{days}天前"
        if days < 365:
            return f"{max(1, days // 30)}个月前"
        return f"{days // 365}年前"
    except Exception:
        return ""


def fmt_duration(seconds) -> str:
    """秒 → 人话时长（"8.2小时" / "45分钟"）；不足 1 分钟返回空（不值得显示）。"""
    try:
        secs = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if secs < 60:
        return ""
    if secs < 3600:
        return f"{secs // 60}分钟"
    return f"{secs / 3600:.1f}小时"
