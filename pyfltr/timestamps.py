"""保存記録に用いるUTC時刻。"""

import datetime as dt


def now_iso() -> str:
    """現在時刻をISO 8601（UTC、マイクロ秒付き）で返す。"""
    return dt.datetime.now(dt.UTC).isoformat()
