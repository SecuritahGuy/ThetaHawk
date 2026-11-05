from __future__ import annotations

from datetime import date

from thetahawk.equity_sample import (
    check_option_expiration_coverage,
    parse_symbol_directories,
)


def test_symbol_directories_keep_plain_common_stock_symbols_only() -> None:
    nasdaq = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAA|Acme Corporation - Common Stock|Q|N|N|100|N|N
AAB|Example Exchange Traded Fund|Q|N|N|100|Y|N
AAC|Test Corporation - Common Stock|Q|Y|N|100|N|N
AAW|Acme Corporation - Warrants|Q|N|N|100|N|N
File Creation Time: 1105202615:00|||||||
"""
    other = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
BBB|Beta Corporation - Common Stock|N|BBB|N|100|N|BBB
BBC|Beta Corporation - Preferred Stock|N|BBC|N|100|N|BBC
DDD.A|Delta Corporation Class A|N|DDD.A|N|100|N|DDD.A
UTF|Cohen & Steers Infrastructure Fund, Inc Common Stock|N|UTF|N|100|N|UTF
TRST|TrustCo Bank Corp NY - Common Stock|N|TRST|N|100|N|TRST
File Creation Time: 1105202615:00|||||||
"""

    records = parse_symbol_directories(nasdaq, other)

    assert [(record.symbol, record.listing_source) for record in records] == [
        ("AAA", "nasdaqlisted.txt"),
        ("BBB", "otherlisted.txt"),
        ("TRST", "otherlisted.txt"),
    ]


def test_option_coverage_checks_listing_without_fetching_quotes() -> None:
    class FakeProvider:
        name = "fake-options"

        def get_option_expirations(self, symbol, *, start, end):
            assert start == date(2026, 11, 19)
            assert end == date(2026, 12, 20)
            if symbol == "BAD":
                raise ConnectionError("unavailable")
            return [date(2026, 11, 20)] if symbol == "OPT" else []

    sleeps = []
    result = check_option_expiration_coverage(
        ["OPT", "NOOPT", "BAD"],
        provider=FakeProvider(),
        as_of=date(2026, 11, 5),
        request_spacing_seconds=0.2,
        sleep=sleeps.append,
    )

    assert result["symbols_with_expirations"] == 1
    assert result["symbols_without_expirations"] == 1
    assert result["quotes_fetched"] is False
    assert result["failures"][0]["symbol"] == "BAD"
    assert sleeps == [0.2, 0.2]
