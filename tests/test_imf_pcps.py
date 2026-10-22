import pytest

from core.config import load_config
from scrapers import imf_pcps as imf

HEADER = ("STRUCTURE[;],STRUCTURE_ID,ACTION,COUNTRY,INDICATOR,DATA_TRANSFORMATION,FREQUENCY,"
          "TIME_PERIOD,OBS_VALUE,SCALE,PRECISION,DECIMAL_DISPLAYED,CURRENCY,COMMODITY_CF,PCPS_RY,"
          "INDEX_TYPE,PRICES,EXRATE,TRANSFORMATION,DERIVATION_TYPE,OVERLAP")


def record(period, value, indicator="PPALLA", transformation="USD", scale="0"):
    return (f"dataflow,IMF.RES:PCPS(9.0.0),R,G001,{indicator},{transformation},M,{period},{value},"
            f"{scale},,,,,,,,,,R,")


PALLADIUM_CSV = "\n".join([HEADER, record("1992-M01", "82.93695652173913"),
                           record("2026-M08", ""), record("2026-M09", "1309.045454545455")])


class FakeFetcher:
    def __init__(self, text=PALLADIUM_CSV, fail=False):
        self.text, self.fail = text, fail
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        if self.fail:
            raise RuntimeError("boom")
        return self.text


def test_series_url():
    assert imf.series_url("PPALLA") == (
        "https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.RES/PCPS/+/G001.PPALLA.USD.M")
    assert imf.series_url("PPALLA", "2026-01").endswith("G001.PPALLA.USD.M?c%5BTIME_PERIOD%5D=ge:2026-01")


def test_parses_periods():
    assert imf.parse_period("2026-M09") == "2026-09-01"
    for bad in ("2026-09", "2026-M13", "2026M09"):
        with pytest.raises(ValueError):
            imf.parse_period(bad)


def test_reads_prices_and_skips_blanks():
    assert imf.parse_series(PALLADIUM_CSV, "palladium") == [
        {"metal": "palladium", "commodity": "Palladium", "market": "Monthly average",
         "unit": "USD/oz", "date": "1992-01-01", "price": 82.93695652173913},
        {"metal": "palladium", "commodity": "Palladium", "market": "Monthly average",
         "unit": "USD/oz", "date": "2026-09-01", "price": 1309.045454545455},
    ]


@pytest.mark.parametrize("bad", [
    record("2026-M09", "213.5", transformation="INDEX"),
    record("2026-M09", "1309", indicator="PPLAT"),
    record("2026-M09", "1.309", scale="3"),
])
def test_another_series_or_a_scale_fails(bad):
    with pytest.raises(ValueError):
        imf.parse_series("\n".join([HEADER, bad]), "palladium")


def test_run(monkeypatch):
    f = FakeFetcher()
    monkeypatch.setattr(imf, "_Fetcher", lambda pacer: f)
    rows = imf.run({})
    assert [r["date"] for r in rows] == ["1992-01-01", "2026-09-01"]
    assert f.requested == [imf.series_url("PPALLA")]
    with pytest.raises(ValueError):
        imf.run({"metals": ["rhodium"]})


@pytest.mark.parametrize("fetcher", [FakeFetcher(fail=True), FakeFetcher(text=HEADER)])
def test_failed_or_empty_series_becomes_marker_row(monkeypatch, fetcher):
    monkeypatch.setattr(imf, "_Fetcher", lambda pacer: fetcher)
    rows = imf.run({})
    assert len(rows) == 1 and rows[0]["status"] == "error" and rows[0]["metal"] == "palladium"


def test_job_yaml_is_valid():
    config = load_config("jobs/imf_palladium.yaml")
    assert config is not None
    assert config.script == "scrapers.imf_pcps"
    assert config.output.key == ["commodity", "market", "date"]
    assert config.parameters["metals"] == ["palladium"]
