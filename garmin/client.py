"""Garmin Connect Personal API client.

Auth (from the official Personal API docs + FAQ):
  POST {BASE}/personalapitoken-service/personal-api/personal-api-key/token
  Header: Authorization: ApiKey <key>
  -> access token valid 24h (or until the key expires). No refresh tokens:
     re-call the token endpoint with the key to get a new one.

Endpoint paths/params come from docs/openapi.json (official Swagger spec).
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from . import config
from .models import Activity, DailySummary, HrvDaily, HrvSummary, Sleep, Token

log = logging.getLogger(__name__)

TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
TOKEN_LIFETIME = timedelta(hours=24)  # per Personal API FAQ
TOKEN_SAFETY_MARGIN = timedelta(minutes=10)

# Documented max date ranges per request
MAX_DAYS_DAILY_SUMMARY = 366  # "up to a maximum of 1 year"
MAX_DAYS_SLEEP = 31  # "up to a maximum of 31 days"
MAX_DAYS_HRV = 31  # not documented; conservative
ACTIVITY_PAGE_SIZE = 99  # documented default limit


class GarminAPIError(RuntimeError):
    def __init__(self, status: int, message: str, url: str = ""):
        super().__init__(f"HTTP {status} for {url}: {message[:500]}")
        self.status = status
        self.url = url


def _parse_token_response(data: dict[str, Any], now: datetime) -> Token:
    """The token response shape isn't documented, so accept common variants."""
    tok = (
        data.get("access_token")
        or data.get("accessToken")
        or data.get("token")
        or data.get("personalApiToken")
    )
    if not tok:
        raise GarminAPIError(200, f"No access token in response keys {sorted(data)}", TOKEN_PATH)

    expires_at: datetime | None = None
    for k in ("expires_in", "expiresIn", "expiresInSeconds"):
        if isinstance(data.get(k), (int, float)):
            expires_at = now + timedelta(seconds=float(data[k]))
            break
    if expires_at is None:
        for k in ("expires_at", "expiresAt", "expiration", "expirationTime", "expiry"):
            v = data.get(k)
            if isinstance(v, (int, float)):  # epoch s or ms
                expires_at = datetime.fromtimestamp(v / 1000 if v > 1e11 else v, UTC)
                break
            if isinstance(v, str):
                try:
                    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                    expires_at = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
                    break
                except ValueError:
                    pass
    if expires_at is None:
        expires_at = now + TOKEN_LIFETIME
    return Token(access_token=tok, expires_at=expires_at)


def _token_from_jwt(jwt: str, now: datetime) -> Token:
    """Token endpoint returns the raw JWT as text/plain; expiry comes from its `exp` claim.
    (The signature is not verified - we only read our own token's expiry.)"""
    import base64

    parts = jwt.split(".")
    if len(parts) != 3:
        raise GarminAPIError(200, "token response is neither JSON nor a JWT", TOKEN_PATH)
    expires_at = now + TOKEN_LIFETIME
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        if isinstance(exp, (int, float)):
            expires_at = datetime.fromtimestamp(exp, UTC)
    except ValueError:
        pass
    return Token(access_token=jwt, expires_at=expires_at)


class GarminClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        token_cache: Path | None = None,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ):
        self.api_key = api_key or config.api_key()
        self.base_url = (base_url or config.BASE_URL).rstrip("/")
        self.token_cache = token_cache if token_cache is not None else config.TOKEN_CACHE
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._sleep = sleep
        self._token: Token | None = None
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "garmin-personal-dashboard/0.1"},
        )

    # ------------------------------------------------------------------ token
    def _load_cached_token(self) -> Token | None:
        try:
            data = json.loads(self.token_cache.read_text())
            # bind cache to this key (hash prefix) so a new key invalidates it
            if data.get("key_fp") != self._key_fp():
                return None
            return Token.model_validate(data)
        except (FileNotFoundError, ValueError, KeyError):
            return None

    def _save_token(self, token: Token) -> None:
        try:
            self.token_cache.parent.mkdir(parents=True, exist_ok=True)
            payload = token.model_dump(mode="json") | {"key_fp": self._key_fp()}
            fd = os.open(self.token_cache, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
        except OSError as e:  # cache is best-effort
            log.warning("could not cache token: %s", e)

    def _key_fp(self) -> str:
        import hashlib

        return hashlib.sha256(self.api_key.encode()).hexdigest()[:16]

    @staticmethod
    def _valid(token: Token | None) -> bool:
        return token is not None and token.expires_at - TOKEN_SAFETY_MARGIN > datetime.now(UTC)

    def fetch_token(self) -> Token:
        resp = self._send_with_retry(
            "POST", TOKEN_PATH,
            # token endpoint replies text/plain; Accept: application/json gets HTTP 406
            headers={"Authorization": f"ApiKey {self.api_key}", "Accept": "*/*"},
        )
        if resp.status_code != 200:
            raise GarminAPIError(resp.status_code, resp.text, TOKEN_PATH)
        body = resp.text.strip()
        if body.startswith("{"):
            token = _parse_token_response(resp.json(), datetime.now(UTC))
        else:  # observed live: text/plain body containing the JWT itself
            token = _token_from_jwt(body, datetime.now(UTC))
        self._token = token
        self._save_token(token)
        log.info("obtained new access token, expires %s", token.expires_at.isoformat())
        return token

    def token(self, force_refresh: bool = False) -> str:
        if not force_refresh:
            if self._valid(self._token):
                return self._token.access_token  # type: ignore[union-attr]
            cached = self._load_cached_token()
            if self._valid(cached):
                self._token = cached
                return cached.access_token  # type: ignore[union-attr]
        return self.fetch_token().access_token

    # ---------------------------------------------------------------- transport
    def _backoff_delay(self, attempt: int, retry_after: str | None) -> float:
        backoff = min(self.backoff_max, self.backoff_base * (2**attempt)) * (0.5 + random.random() / 2)
        if retry_after:
            try:
                # Garmin has been seen sending Retry-After: 0; never retry faster than our backoff
                return max(backoff, min(float(retry_after), self.backoff_max * 5))
            except ValueError:
                pass
        return backoff

    def _send_with_retry(self, method: str, path: str, **kw) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.request(method, path, **kw)
            except (httpx.TransportError,) as e:
                last_exc = e
                if attempt == self.max_retries:
                    raise
                d = self._backoff_delay(attempt, None)
                log.warning("%s %s network error %s; retry in %.1fs", method, path, e, d)
                self._sleep(d)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_retries:
                    return resp
                d = self._backoff_delay(attempt, resp.headers.get("Retry-After"))
                log.warning("%s %s -> %s; retry in %.1fs", method, path, resp.status_code, d)
                self._sleep(d)
                continue
            return resp
        raise last_exc or RuntimeError("unreachable")

    def request(self, method: str, path: str, params: dict | None = None,
                accept: str = "application/json") -> httpx.Response:
        """Authenticated request; on 401 refreshes the token once and retries.
        Garmin answers 406 if Accept doesn't match the payload type, so binary calls pass accept."""
        for refreshed in (False, True):
            headers = {"Authorization": f"Bearer {self.token(force_refresh=refreshed)}", "Accept": accept}
            resp = self._send_with_retry(method, path, params=params, headers=headers)
            if resp.status_code == 401 and not refreshed:
                log.info("401 from %s; refreshing token", path)
                continue
            return resp
        return resp  # pragma: no cover

    def get_json(self, path: str, params: dict | None = None) -> Any:
        resp = self.request("GET", path, params=params)
        if resp.status_code == 204:
            return None
        if resp.status_code != 200:
            raise GarminAPIError(resp.status_code, resp.text, path)
        return resp.json() if resp.content else None

    # ---------------------------------------------------------------- endpoints
    def daily_summaries(self, start: date, end: date) -> list[tuple[DailySummary, dict]]:
        data = self.get_json(
            "/usersummary-service/v2/dailysummaries",
            {"startDate": start.isoformat(), "endDate": end.isoformat()},
        )
        items = (data or {}).get("data", []) if isinstance(data, dict) else (data or [])
        return [(DailySummary.model_validate(d), d) for d in items]

    def sleep_summaries(self, start: date, end: date) -> list[tuple[Sleep, dict]]:
        data = self.get_json(
            "/sleep-service/sleep/dailySleepSummariesByDate",
            {"startDate": start.isoformat(), "endDate": end.isoformat()},
        )
        return [(Sleep.model_validate(d), d) for d in (data or []) if d.get("calendarDate")]

    def hrv_daily(self, start: date, end: date) -> list[tuple[HrvSummary, dict]]:
        data = self.get_json(f"/hrv-service/hrv/daily/{start.isoformat()}/{end.isoformat()}")
        if not data:
            return []
        raw = data.get("hrvSummaries", []) if isinstance(data, dict) else []
        parsed = HrvDaily.model_validate(data)
        return list(zip(parsed.hrvSummaries, raw))

    def activities(self, start: date, end: date) -> list[tuple[Activity, dict]]:
        out: list[tuple[Activity, dict]] = []
        offset = 0
        while True:
            page = self.get_json(
                "/activitylist-service/activities/search/activities",
                {
                    "startDate": start.isoformat(),
                    "endDate": end.isoformat(),
                    "start": offset,
                    "limit": ACTIVITY_PAGE_SIZE,
                    "sortOrder": "asc",
                },
            ) or []
            out.extend((Activity.model_validate(a), a) for a in page)
            if len(page) < ACTIVITY_PAGE_SIZE:
                return out
            offset += len(page)

    def activity_detail(self, activity_id: int) -> dict:
        return self.get_json(f"/activity-service/activity/{activity_id}")

    def download_activity_file(self, activity_id: int) -> bytes:
        """Original activity file; the docs say the response is application/x-zip-compressed."""
        resp = self.request("GET", f"/download-service/files/activity/{activity_id}", accept="*/*")
        if resp.status_code != 200:
            raise GarminAPIError(resp.status_code, resp.text, f"download {activity_id}")
        return resp.content

    # ---- v2 endpoints (all paths from docs/openapi.json) ----
    def weight_range(self, start: date, end: date) -> list[dict]:
        """All weigh-ins in [start, end] (flattened from dailyWeightSummaries[].allWeightMetrics)."""
        data = self.get_json(f"/weight-service/weight/range/{start.isoformat()}/{end.isoformat()}") or {}
        out = []
        for day in data.get("dailyWeightSummaries") or []:
            out.extend(day.get("allWeightMetrics") or ([day["latestWeight"]] if day.get("latestWeight") else []))
        return out

    def body_battery_reports(self, start: date, end: date, chunks: int = 48) -> list[dict]:
        """Daily Body Battery reports (max 31 days). `chunks` = intervals per day (48 = 30 min)."""
        return self.get_json("/wellness-service/wellness/bodyBattery/reports/daily",
                             {"startDate": start.isoformat(), "endDate": end.isoformat(), "chunks": chunks}) or []

    def daily_heart_rate(self, day: date) -> dict | None:
        return self.get_json("/wellness-service/wellness/dailyHeartRate", {"date": day.isoformat()})

    def spo2_acclimation(self, day: date) -> dict | None:
        return self.get_json(f"/wellness-service/wellness/daily/spo2acclimation/{day.isoformat()}")

    def fitness_age(self, as_of: date) -> dict | None:
        data = self.get_json(f"/fitnessage-service/fitnessage/v2/{as_of.isoformat()}")
        if isinstance(data, list):  # spec says array; live API returns an object
            data = data[0] if data else None
        return data

    def heart_rate_zones(self) -> list[dict]:
        return self.get_json("/biometric-service/heartRateZones/v2") or []

    def user_settings(self) -> dict:
        return self.get_json("/userprofile-service/userprofile/settings") or {}

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
