from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from garmin.client import TOKEN_PATH, GarminAPIError, GarminClient, _parse_token_response
from garmin.models import Token

BASE = "https://api.test"


@pytest.fixture
def client(tmp_path):
    sleeps: list[float] = []
    cl = GarminClient(api_key="k123", base_url=BASE, token_cache=tmp_path / "tok.json",
                      max_retries=3, sleep=sleeps.append)
    cl._sleeps = sleeps  # type: ignore[attr-defined]
    yield cl
    cl.close()


def _token_route(mock, token="tok-1", **extra):
    return mock.post(f"{BASE}{TOKEN_PATH}").mock(
        return_value=httpx.Response(200, json={"access_token": token, "expires_in": 86400, **extra}))


@respx.mock
def test_token_fetch_sends_apikey_header_and_caches(client, tmp_path):
    route = _token_route(respx)
    assert client.token() == "tok-1"
    assert route.calls[0].request.headers["Authorization"] == "ApiKey k123"
    # second call served from memory; new client served from file cache
    assert client.token() == "tok-1"
    cl2 = GarminClient(api_key="k123", base_url=BASE, token_cache=tmp_path / "tok.json")
    assert cl2.token() == "tok-1"
    assert route.call_count == 1
    # a different key must not reuse the cache
    cl3 = GarminClient(api_key="other", base_url=BASE, token_cache=tmp_path / "tok.json")
    cl3.token()
    assert route.call_count == 2


def test_token_parse_variants():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    t = _parse_token_response({"accessToken": "a", "expiresIn": 60}, now)
    assert t.access_token == "a" and t.expires_at == now + timedelta(seconds=60)
    t = _parse_token_response({"token": "b"}, now)
    assert t.expires_at == now + timedelta(hours=24)  # FAQ default
    with pytest.raises(GarminAPIError):
        _parse_token_response({"nope": 1}, now)


@respx.mock
def test_token_plain_text_jwt(client):
    """Live API returns text/plain with the JWT itself; expiry is read from `exp`."""
    import base64
    import json as _json
    exp = int((datetime.now(UTC) + timedelta(hours=24)).timestamp())
    seg = lambda d: base64.urlsafe_b64encode(_json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    jwt = f"{seg({'alg': 'RS256'})}.{seg({'exp': exp, 'scope': ['X']})}.sig"
    respx.post(f"{BASE}{TOKEN_PATH}").mock(
        return_value=httpx.Response(200, text=jwt, headers={"content-type": "text/plain"}))
    tok = client.fetch_token()
    assert tok.access_token == jwt
    assert int(tok.expires_at.timestamp()) == exp


@respx.mock
def test_expired_cached_token_is_refreshed(client):
    route = _token_route(respx, token="fresh")
    client._save_token(Token(access_token="old", expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    assert client.token() == "fresh"
    assert route.call_count == 1


@respx.mock
def test_401_triggers_single_token_refresh(client):
    respx.post(f"{BASE}{TOKEN_PATH}").mock(side_effect=[
        httpx.Response(200, json={"access_token": "t1"}),
        httpx.Response(200, json={"access_token": "t2"}),
    ])
    data = respx.get(f"{BASE}/sleep-service/sleep/dailySleepSummariesByDate").mock(side_effect=[
        httpx.Response(401),
        httpx.Response(200, json=[{"calendarDate": "2026-01-02", "sleepTimeSeconds": 28000}]),
    ])
    out = client.sleep_summaries(date(2026, 1, 1), date(2026, 1, 2))
    assert out[0][0].sleepTimeSeconds == 28000
    assert [c.request.headers["Authorization"] for c in data.calls] == ["Bearer t1", "Bearer t2"]


@respx.mock
def test_429_retries_with_retry_after(client):
    _token_route(respx)
    route = respx.get(f"{BASE}/hrv-service/hrv/daily/2026-01-01/2026-01-31").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(503),
        httpx.Response(200, json={"hrvSummaries": [{"calendarDate": "2026-01-05", "lastNightAvg": 48}]}),
    ])
    out = client.hrv_daily(date(2026, 1, 1), date(2026, 1, 31))
    assert route.call_count == 3
    assert client._sleeps[0] == 7.0
    assert out[0][0].lastNightAvg == 48 and out[0][1]["lastNightAvg"] == 48


@respx.mock
def test_retries_exhausted_raises(client):
    _token_route(respx)
    respx.get(f"{BASE}/usersummary-service/v2/dailysummaries").mock(return_value=httpx.Response(500))
    with pytest.raises(GarminAPIError) as e:
        client.daily_summaries(date(2026, 1, 1), date(2026, 1, 2))
    assert e.value.status == 500
    assert len(client._sleeps) == 3


@respx.mock
def test_hrv_204_returns_empty(client):
    _token_route(respx)
    respx.get(f"{BASE}/hrv-service/hrv/daily/2026-01-01/2026-01-02").mock(return_value=httpx.Response(204))
    assert client.hrv_daily(date(2026, 1, 1), date(2026, 1, 2)) == []


@respx.mock
def test_daily_summaries_typed(client):
    _token_route(respx)
    respx.get(f"{BASE}/usersummary-service/v2/dailysummaries").mock(return_value=httpx.Response(200, json={
        "data": [{"calendarDate": "2026-01-01", "movement": {"steps": {"value": 9000, "goal": 8000}},
                  "heartRate": {"restingValue": 52}, "unknownField": {"x": 1}}]}))
    (m, raw), = client.daily_summaries(date(2026, 1, 1), date(2026, 1, 1))
    assert m.movement.steps.value == 9000 and m.heartRate.restingValue == 52
    assert raw["unknownField"] == {"x": 1}


@respx.mock
def test_activities_paginate(client):
    _token_route(respx)
    page1 = [{"activityId": i} for i in range(99)]
    page2 = [{"activityId": 1000}]
    route = respx.get(f"{BASE}/activitylist-service/activities/search/activities").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)])
    out = client.activities(date(2026, 1, 1), date(2026, 1, 31))
    assert len(out) == 100
    assert route.calls[1].request.url.params["start"] == "99"
    assert route.calls[0].request.url.params["startDate"] == "2026-01-01"


@respx.mock
def test_download_sends_wildcard_accept(client):
    _token_route(respx)
    route = respx.get(f"{BASE}/download-service/files/activity/42").mock(
        return_value=httpx.Response(200, content=b"PK\x03\x04zip"))
    assert client.download_activity_file(42).startswith(b"PK")
    assert route.calls[0].request.headers["Accept"] == "*/*"
    assert route.calls[0].request.headers["Authorization"].startswith("Bearer ")


@respx.mock
def test_network_error_retried(client):
    _token_route(respx)
    respx.get(f"{BASE}/sleep-service/sleep/dailySleepSummariesByDate").mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json=[])])
    assert client.sleep_summaries(date(2026, 1, 1), date(2026, 1, 2)) == []
