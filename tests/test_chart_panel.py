"""Regression: a single candle must render on Windows at varying sizes."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest
from textual.app import App, ComposeResult

from stock_terminal.api.models import Candle
from stock_terminal.tui.widgets.chart_panel import ChartPanel


class Harness(App):
    def compose(self) -> ComposeResult:
        yield ChartPanel()


@pytest.mark.parametrize("size", [(80, 24), (48, 12)])
@pytest.mark.parametrize("interval", ["1d", "1m"])
@pytest.mark.parametrize("mode", ["candle", "line"])
async def test_single_candle_and_following_series_render(size, interval, mode):
    app = Harness()
    async with app.run_test(size=size) as pilot:
        chart = app.query_one(ChartPanel)
        chart.interval = interval
        chart.mode = mode
        stamp = datetime(2026, 10, 27, 10, tzinfo=timezone.utc)
        candle = Candle(stamp, *(Decimal("100"),)*5)
        chart.show_candles("TEST", [candle])
        await pilot.pause()
        assert chart.render() is not None
        following = Candle(stamp + timedelta(days=1), *(Decimal("101"),)*5)
        chart.show_candles("TEST", [candle, following])
        await pilot.pause()
        assert chart.render() is not None
