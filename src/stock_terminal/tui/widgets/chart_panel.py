"""Candle/line chart for the currently selected watchlist symbol."""

from __future__ import annotations

from textual_plotext import PlotextPlot

from stock_terminal.api.models import Candle

_DATE_FORM_1D = "Y-m-d"
_DATE_FORM_1M = "Y-m-d H:M:S"
_STRFTIME_1D = "%Y-%m-%d"
_STRFTIME_1M = "%Y-%m-%d %H:%M:%S"


class ChartPanel(PlotextPlot):
    """Wraps textual-plotext's PlotextPlot to show OHLC candles for one symbol."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.symbol: str | None = None
        self.interval: str = "1d"
        self.mode: str = "candle"  # "candle" or "line"
        self._candles: list[Candle] = []

    def on_mount(self) -> None:
        self.plt.date_form(_DATE_FORM_1D)
        self._show_placeholder("종목을 선택하세요 (관심목록에서 커서 이동)")

    def toggle_interval(self) -> None:
        self.interval = "1m" if self.interval == "1d" else "1d"

    def toggle_mode(self) -> None:
        self.mode = "line" if self.mode == "candle" else "candle"
        self._replot()

    def show_candles(self, symbol: str, candles: list[Candle]) -> None:
        self.symbol = symbol
        self._candles = candles
        self._replot()

    def _show_placeholder(self, message: str) -> None:
        self.plt.clear_data()
        self.plt.title(message)
        self.refresh()

    def _replot(self) -> None:
        if not self._candles or self.symbol is None:
            self._show_placeholder("데이터 없음")
            return

        strftime_fmt = _STRFTIME_1M if self.interval == "1m" else _STRFTIME_1D
        date_form = _DATE_FORM_1M if self.interval == "1m" else _DATE_FORM_1D

        self.plt.clear_data()
        self.plt.date_form(date_form)
        interval_label = "1분봉" if self.interval == "1m" else "일봉"
        self.plt.title(f"{self.symbol} ({interval_label})")

        dates = [c.timestamp.strftime(strftime_fmt) for c in self._candles]

        if self.mode == "candle":
            data = {
                "Open": [float(c.open) for c in self._candles],
                "Close": [float(c.close) for c in self._candles],
                "High": [float(c.high) for c in self._candles],
                "Low": [float(c.low) for c in self._candles],
            }
            self.plt.candlestick(dates, data)
        else:
            closes = [float(c.close) for c in self._candles]
            self.plt.plot(dates, closes, marker="braille")

        self.refresh()
