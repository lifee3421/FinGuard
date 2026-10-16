# Demo market fixture

The canonical offline quote fixture is `market.json`. The provider loads its
supported symbols, source metadata, scenario quotes, and scenario clock from
that file. It is deterministic, synthetic, and contains AAPL, MSFT, and NVDA
only. The trusted scenario values are:

- `SCENARIO_T0`: research/proposal quote
- `SCENARIO_T1` / `normal`: approval quote within the 3% execution gap
- `price_gap`: approval quote beyond the 3% gap
- `stale` and `unavailable`: invalid approval quote variants

No model input can select a scenario. A test or trusted demo service chooses it
when constructing `DemoMarketDataProvider`.

Historical rows are generated deterministically from the T0 quote in
`market.json`, ending on the fixture's weekday `as_of` date. They are synthetic
weekday observations, not actual market history. The date generator excludes
weekends but does not model exchange holidays.
