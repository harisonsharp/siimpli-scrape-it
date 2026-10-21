import pytest

from core.config import load_config
from scrapers import ecb_exchange_rates as ecb

DATA_HEADER = "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"
UNITS_HEADER = ("KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,"
                "OBS_STATUS,UNIT,UNIT_MULT\n")


def data_csv(currency, freq, points):
    return DATA_HEADER + "".join(
        f"EXR.{freq}.{currency}.EUR.SP00.A,{freq},{currency},EUR,SP00,A,{p},{v}\n" for p, v in points)


def units_csv(currencies, unit=None, mult="0"):
    return UNITS_HEADER + "".join(
        f"EXR.D.{c}.EUR.SP00.A,D,{c},EUR,SP00,A,2026-10-20,1.0,A,{unit or c},{mult}\n" for c in currencies)


class FakeFetcher:
    def __init__(self, units=None, fail=()):
        self.units = units
        self.fail = set(fail)
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        key = url.split("/EXR/")[1].split("?")[0]
        freq, currencies = key.split(".")[:2]
        if "lastNObservations=1" in url:
            return self.units if self.units is not None else units_csv(currencies.split("+"))
        if currencies in self.fail:
            raise RuntimeError("boom")
        if freq == "M":
            return data_csv(currencies, "M", [("2026-08", "1.1593"), ("2026-09", "1.1513")])
        return data_csv(currencies, "D", [("2000-04-21", ""), ("2026-10-19", "1.1225"), ("2026-10-20", "1.1215")])


@pytest.fixture
def fake(monkeypatch):
    f = FakeFetcher()
    monkeypatch.setattr(ecb, "_Fetcher", lambda pacer: f)
    return f


def test_parses_periods():
    assert ecb.parse_period("2026-10-20", "daily") == "2026-10-20"
    assert ecb.parse_period("2026-09", "monthly") == "2026-09-01"
    with pytest.raises(ValueError):
        ecb.parse_period("2026-09", "daily")
    with pytest.raises(ValueError):
        ecb.parse_period("2026-Q3", "monthly")


def test_parses_a_series_and_skips_blank_days():
    text = data_csv("CNY", "D", [("2000-04-21", ""), ("2000-04-25", "NaN"), ("2026-10-20", "7.4894")])
    assert ecb.parse_series(text, "CNY", "daily") == [
        {"currency": "CNY", "base": "EUR", "frequency": "daily", "unit": "CNY per 1 EUR",
         "date": "2026-10-20", "rate": 7.4894},
    ]


def test_another_series_in_the_response_fails():
    with pytest.raises(ValueError):
        ecb.parse_series(data_csv("GBP", "D", [("2026-10-20", "0.84")]), "USD", "daily")
    with pytest.raises(ValueError):
        ecb.parse_series(data_csv("USD", "M", [("2026-09", "1.15")]), "USD", "daily")


def test_fetches_both_frequencies_of_every_currency(fake):
    rows = ecb.run({})
    assert all("status" not in r for r in rows)
    assert {r["currency"] for r in rows} == set(ecb.CURRENCIES)
    assert {"currency": "GBP", "base": "EUR", "frequency": "monthly", "unit": "GBP per 1 EUR",
            "date": "2026-09-01", "rate": 1.1513} in rows
    assert len(rows) == len(ecb.CURRENCIES) * 4
    assert len(fake.requested) == 1 + 2 * len(ecb.CURRENCIES)
    assert all("detail=dataonly" in u for u in fake.requested[1:])


def test_currency_frequency_and_start_filters(fake):
    rows = ecb.run({"currencies": ["usd", "cad"], "frequencies": ["monthly"], "start_period": "2026-08"})
    assert {(r["currency"], r["frequency"]) for r in rows} == {("USD", "monthly"), ("CAD", "monthly")}
    assert all("startPeriod=2026-08" in u for u in fake.requested[1:])
    with pytest.raises(ValueError):
        ecb.run({"frequencies": ["weekly"]})


def test_failed_series_becomes_marker_row(monkeypatch):
    f = FakeFetcher(fail={"CAD"})
    monkeypatch.setattr(ecb, "_Fetcher", lambda pacer: f)
    rows = ecb.run({"currencies": ["CAD", "USD"]})
    assert any(r.get("status") == "error" and r["currency"] == "CAD" for r in rows)
    assert any(r.get("currency") == "USD" and r.get("date") == "2026-10-20" for r in rows)


@pytest.mark.parametrize("units", [
    units_csv(["USD"], mult="3"),
    units_csv(["USD"], unit="EUR"),
    units_csv(["GBP"]),
])
def test_unit_change_or_missing_currency_fails_the_run(monkeypatch, units):
    f = FakeFetcher(units=units)
    monkeypatch.setattr(ecb, "_Fetcher", lambda pacer: f)
    with pytest.raises(ValueError):
        ecb.run({"currencies": ["USD"]})


def test_job_yaml_is_valid():
    config = load_config("jobs/ecb_exchange_rates.yaml")
    assert config is not None
    assert config.script == "scrapers.ecb_exchange_rates"
    assert config.output.key == ["currency", "frequency", "date"]
    assert config.parameters["currencies"] == list(ecb.CURRENCIES)
    assert config.parameters["frequencies"] == list(ecb.FREQUENCIES)
