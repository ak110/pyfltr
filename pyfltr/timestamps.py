import datetime as dt


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()
