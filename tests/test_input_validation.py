"""Regression coverage for unsafe intervals and non-finite market values."""
from decimal import Decimal

import httpx
import pytest
import respx

from stock_terminal.config import Settings, ConfigError
from stock_terminal.api.client import TossInvestClient
from stock_terminal.api.exceptions import TossAPIError
from stock_terminal.api.models import Price, Candle


@pytest.mark.parametrize("field", ["POLL_INTERVAL_SECONDS", "CHART_REFRESH_SECONDS"])
@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "-Infinity", "abc", "", "1e999"])
def test_invalid_environment_interval_is_config_error(monkeypatch, field, value):
    monkeypatch.setattr("stock_terminal.config.load_dotenv", lambda: None)
    for name, default in {"TOSS_CLIENT_ID":"test", "TOSS_CLIENT_SECRET":"test",
                          "POLL_INTERVAL_SECONDS":"3", "CHART_REFRESH_SECONDS":"30"}.items():
        monkeypatch.setenv(name, default)
    monkeypatch.setenv(field, value)
    with pytest.raises(ConfigError, match=field):
        Settings.load()


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, None, 10**1000])
def test_direct_settings_creation_rejects_invalid_intervals(value):
    with pytest.raises(ConfigError):
        Settings("id", "secret", value, 30)


def test_valid_fractional_and_default_intervals(monkeypatch):
    monkeypatch.setattr("stock_terminal.config.load_dotenv", lambda: None)
    monkeypatch.setenv("TOSS_CLIENT_ID", "test")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "test")
    monkeypatch.delenv("POLL_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("CHART_REFRESH_SECONDS", raising=False)
    assert Settings.load().poll_interval_seconds == 3
    assert Settings.load().chart_refresh_seconds == 30
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0.5")
    assert Settings.load().poll_interval_seconds == 0.5


@pytest.mark.parametrize("parser", [Price.from_json, Price.from_market_indicator_json])
@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity", "0", "-1", "1e999", "1e-999", True, None])
def test_invalid_prices_rejected_by_both_parsers(parser, value):
    with pytest.raises((ValueError, ArithmeticError)):
        parser({"symbol":"TEST", "lastPrice":value, "timestamp":None, "currency":"USD"})


def candle_data():
    return {"timestamp":"2026-10-30T10:00:00+09:00", "openPrice":"100",
            "highPrice":"110", "lowPrice":"90", "closePrice":"105", "volume":"0"}


@pytest.mark.parametrize("field", ["openPrice", "highPrice", "lowPrice", "closePrice", "volume"])
@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-1", "1e999", True, None])
def test_invalid_candle_numbers_are_rejected(field, value):
    data = candle_data()
    data[field] = value
    with pytest.raises((ValueError, ArithmeticError)):
        Candle.from_json(data)


@pytest.mark.parametrize("overrides", [{"highPrice":"80"}, {"lowPrice":"120"},
    {"openPrice":"120"}, {"closePrice":"80"}, {"closePrice":"0"}])
def test_invalid_ohlc_bounds_are_rejected(overrides):
    data = candle_data()
    data.update(overrides)
    with pytest.raises(ValueError):
        Candle.from_json(data)


def test_valid_zero_volume_and_price_precision_are_preserved():
    candle = Candle.from_json(candle_data())
    assert candle.volume == 0
    price = Price.from_json({"symbol":"TEST", "lastPrice":"0.01234567", "timestamp":None, "currency":"USD"})
    assert price.last_price == Decimal("0.01234567")


@pytest.mark.parametrize("index", [False, True])
@respx.mock
async def test_client_rejects_bad_quote_then_recovers(index):
    base = "https://openapi.tossinvest.com"
    respx.post(base + "/oauth2/token").respond(200, json={"access_token":"test", "expires_in":3600})
    route = "/api/v1/market-indicators/prices" if index else "/api/v1/prices"
    bad = {"symbol":"TEST", "lastPrice":"NaN", "timestamp":None, "currency":"USD"}
    good = dict(bad, lastPrice="100")
    respx.get(base + route).mock(side_effect=[httpx.Response(200, json={"result":[bad]}),
                                              httpx.Response(200, json={"result":[good]})])
    async with TossInvestClient("id", "secret", base) as client:
        fetch = client.get_market_indicator_prices if index else client.get_prices
        with pytest.raises(TossAPIError):
            await fetch(["TEST"])
        assert (await fetch(["TEST"]))[0].last_price == 100


@pytest.mark.parametrize("index", [False, True])
@respx.mock
async def test_client_rejects_bad_candle_then_recovers(index):
    base = "https://openapi.tossinvest.com"
    respx.post(base + "/oauth2/token").respond(200, json={"access_token":"test", "expires_in":3600})
    route = "/api/v1/market-indicators/KOSPI/candles" if index else "/api/v1/candles"
    data = candle_data()
    respx.get(base + route).mock(side_effect=[
        httpx.Response(200, json={"result":{"candles":[dict(data, highPrice="80")]}}),
        httpx.Response(200, json={"result":{"candles":[data]}})])
    async with TossInvestClient("id", "secret", base) as client:
        fetch = client.get_market_indicator_candles if index else client.get_candles
        with pytest.raises(TossAPIError):
            await fetch("KOSPI")
        assert (await fetch("KOSPI"))[0].close == 105
