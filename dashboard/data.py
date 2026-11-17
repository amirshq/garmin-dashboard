"""Read-only, cached data access for the dashboard."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from garmin import config, insights


@st.cache_resource
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA query_only = ON")
    return conn


@st.cache_data(ttl=120, show_spinner=False)
def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, _conn(), params=params)


def scalar(sql: str, params: tuple = ()):
    df = q(sql, params)
    return None if df.empty else df.iloc[0, 0]


@st.cache_data(ttl=120, show_spinner=False)
def alerts(today_iso: str) -> list:
    return insights.compute_alerts(_conn(), date.fromisoformat(today_iso))


@st.cache_data(ttl=120, show_spinner=False)
def load_series(end_iso: str, days: int) -> pd.DataFrame:
    df = pd.DataFrame(insights.training_load(_conn(), date.fromisoformat(end_iso), days))
    if len(df):
        df["date"] = pd.to_datetime(df["date"])
    return df


@dataclass
class Ctx:
    start: date
    end: date
    data_lo: date
    data_hi: date

    @property
    def span(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def S(self) -> str:
        return self.start.isoformat()

    @property
    def E(self) -> str:
        return self.end.isoformat()

    @property
    def prev(self) -> tuple[str, str]:
        return ((self.start - timedelta(days=self.span)).isoformat(),
                (self.start - timedelta(days=1)).isoformat())

    @property
    def lead(self) -> str:  # 6-day lead so 7-day rolling averages are valid from the first visible day
        return (self.start - timedelta(days=6)).isoformat()

    @property
    def x_range(self) -> tuple[str, str]:
        return (self.S, (self.end + timedelta(hours=12)).isoformat())


def table(name: str, a: str, b: str, date_col: str = "calendar_date", cols: str = "*") -> pd.DataFrame:
    df = q(f"SELECT {cols} FROM {name} WHERE {date_col} BETWEEN ? AND ? ORDER BY {date_col}", (a, b))
    return df.drop(columns=[c for c in ("raw_json", "detail_json") if c in df])


def series(df: pd.DataFrame, col: str, a: str, b: str, scale: float = 1.0, date_col: str = "calendar_date") -> pd.Series:
    idx = pd.date_range(a, b)
    if df is None or not len(df) or col not in df:
        return pd.Series(index=idx, dtype=float)
    s = pd.Series(pd.to_numeric(df[col], errors="coerce").values * scale, index=pd.to_datetime(df[date_col]))
    return s[~s.index.duplicated(keep="last")].reindex(idx)


def mean(df: pd.DataFrame, col: str, a: str | None = None) -> float | None:
    if df is None or not len(df) or col not in df:
        return None
    d = df[df.calendar_date >= a] if a else df
    v = pd.to_numeric(d[col], errors="coerce").dropna()
    return float(v.mean()) if len(v) else None


def profile(kind: str):
    raw = scalar("SELECT raw_json FROM profile_snapshot WHERE kind=?", (kind,))
    return json.loads(raw) if raw else None


def activity_types(a: str, b: str) -> pd.DataFrame:
    return q("SELECT type_key, count(*) n, sum(duration_s)/3600.0 hours FROM activity WHERE calendar_date "
             "BETWEEN ? AND ? GROUP BY 1 ORDER BY n DESC", (a, b))


def pretty_type(t: str | None) -> str:
    if not t:
        return "Other"
    t = t.replace("_v2", "").replace("_ws", "")
    return t.replace("_", " ").capitalize()
