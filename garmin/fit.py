"""Store and parse activity files from /download-service/files/activity/{id}.

The endpoint returns a zip with the activity in its original format (usually .fit;
manual uploads may be .gpx/.tcx). We keep the extracted file and parse FIT session data.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import fitdecode

from . import config


def save_activity_file(activity_id: int, payload: bytes, fit_dir: Path | None = None) -> Path:
    """Extract the zip payload; returns path to the activity file (FIT preferred)."""
    fit_dir = fit_dir or config.FIT_DIR
    fit_dir.mkdir(parents=True, exist_ok=True)
    if payload[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if not names:
                raise ValueError("empty zip")
            names.sort(key=lambda n: (not n.lower().endswith(".fit"), n))
            name = names[0]
            ext = Path(name).suffix.lower() or ".bin"
            out = fit_dir / f"{activity_id}{ext}"
            out.write_bytes(zf.read(name))
            return out
    # Not zipped: assume raw FIT if header says so
    ext = ".fit" if payload[8:12] == b".FIT" else ".bin"
    out = fit_dir / f"{activity_id}{ext}"
    out.write_bytes(payload)
    return out


def parse_fit(path: Path) -> dict[str, Any]:
    """Pull key session-level fields + record count from a FIT file."""
    session: dict[str, Any] = {}
    n_records = 0
    with fitdecode.FitReader(str(path), check_crc=fitdecode.CrcCheck.WARN) as fr:
        for frame in fr:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name == "record":
                n_records += 1
            elif frame.name == "session" and not session:
                for f in frame.fields:
                    session[f.name] = f.value

    def num(*keys):
        for k in keys:
            v = session.get(k)
            if isinstance(v, (int, float)):
                return float(v)
        return None

    def txt(k):
        v = session.get(k)
        return None if v is None else str(v)

    return {
        "sport": txt("sport"),
        "sub_sport": txt("sub_sport"),
        "total_timer_s": num("total_timer_time"),
        "total_distance_m": num("total_distance"),
        "avg_hr_bpm": num("avg_heart_rate"),
        "max_hr_bpm": num("max_heart_rate"),
        "avg_cadence": num("avg_cadence", "avg_running_cadence"),
        "total_ascent_m": num("total_ascent"),
        "n_records": n_records,
    }
