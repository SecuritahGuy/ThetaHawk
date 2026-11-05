# Market-data provider notes

Checked on November 5, 2026. Thetahawk currently has a read-only Alpaca market-data adapter. Provider credentials are optional and are read from environment variables; no subscription is purchased or assumed.

## Current adapter: Alpaca

The adapter requests latest equity quotes and option-chain snapshots. It also reads option contract metadata so open interest and its date can be carried into the normalized quote. It does not call order, account, or position endpoints.

The default settings are `iex` for equities and `indicative` for options. Alpaca describes its free indicative options feed as modified quotes and derivative trades delayed by 15 minutes; OPRA is the consolidated BBO feed and requires an active subscription. Historical option data currently begins in February 2024. These limits make Alpaca useful for wiring and current research, but the free feed and short history are insufficient by themselves for a strong historical-validation phase. The app records the selected feed in each quote and the earnings screen suppresses inputs with missing or stale open-interest dates.

Configure the keys and feeds in a local `.env` file (never commit it):

```text
APCA_API_KEY_ID=...
APCA_API_SECRET_KEY=...
THETAHAWK_ALPACA_STOCK_FEED=iex
THETAHAWK_ALPACA_OPTIONS_FEED=indicative
```

`opra` may be selected only if the account has the needed market-data entitlement. Use `/v2/options/contracts` only for contract metadata; `THETAHAWK_ALPACA_BROKER_URL` defaults to Alpaca's paper API base URL. The adapter itself contains no trading operations.

`THETAHAWK_ALPACA_BROKER_URL` accepts either the host root (`https://paper-api.alpaca.markets`) or a versioned base (`https://paper-api.alpaca.markets/v2`). The contract metadata adapter normalizes both forms.

## Other options

| Provider | Useful capability | Limitation for this project |
| --- | --- | --- |
| Tradier | Brokerage accounts include API access and real-time consolidated Level 1 options market data; paper sandbox is available. | Historical data is not available for expired options, which blocks the main event-time backtest. API tokens are full account access and must be protected. |
| Massive | Options Basic is free with end-of-day data and a 5-call/minute limit. Paid options tiers add delayed data, history, Greeks/IV, open interest, snapshots, and eventually real-time quotes. | Historical coverage and quote entitlements vary by plan. Advanced real-time access is listed at $199/month; verify exact plan fields and market-data rights before subscribing. |

Current Massive individual plans list Options Starter at $29/month with 15-minute delayed data and two years of history; Developer at $79/month with four years; and Advanced at $199/month with real-time data and five-plus years. For eventual backtesting, Massive appears stronger than Alpaca's currently documented history, but don't buy a plan until the test design identifies the exact quote fields and dates required.

## Next provider milestone

The first scan uses operator-supplied CSVs for upcoming earnings and past realized moves. `earnings.csv` requires symbol, event date, before/after-market timing, chosen option expiration, timezone-aware `observed_at`, and source. `realized_moves.csv` requires symbol, event date, pre-event reference price, post-event price, timezone-aware `observed_at`, and source; Thetahawk derives the percentage move from the two prices. Use one consistent event window (for example, prior regular-session close to the first full regular-session close after the announcement), apply corporate-action adjustments, and record the source. Example headers are in `examples/`. Earnings dates are not inferred from option symbols or LLM prose. A calendar API adapter can replace the CSV input later without changing scanner calculations.

## Sources

- [Alpaca option-chain API](https://docs.alpaca.markets/us/reference/optionchain) — fields, pagination, feed selection, and authentication.
- [Alpaca option-contract metadata API](https://docs.alpaca.markets/us/reference/get-options-contracts) — contract status and open-interest date/value.
- [Alpaca historical options data](https://docs.alpaca.markets/us/docs/historical-option-data) — history begins February 2024 and feed distinctions.
- [Tradier market-data documentation](https://docs.tradier.com/brokerage-api/markets) — real-time data, sandbox, access scope, and expired-options history limitation.
- [Massive options pricing](https://www.massive.com/pricing?product=options) — current tiers and plan feature matrix.
