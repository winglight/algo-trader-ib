# Local bar downloads
Important execution boundary: downloaded Parquet is native-reader compatible, but the current Cloud-dispatched local connector receives signed dataChunks and prepares SQLite inputs. Selecting a local worker in Studio does not automatically substitute these files. Do not replace frozen inputs or claim local-file execution until the actual native reader path is selected and verified.

The installer does not download market data. The LTB page only reads local coverage and downloads these files; it never accepts keys or runs commands.

## Run in the installed research image
From your public installation directory (Docker Compose v2):
```sh
./scripts/download_market_data.sh --provider alpaca --symbols SPY,QQQ --timeframes 1m,5m --start 2025-01-01T00:00:00Z --end 2025-02-01T00:00:00Z
```
The default only prints a plan. Add `--execute` to request downloads yourself. Omitting symbol/timeframe lists in an interactive terminal prompts for them. `--status` reads observed local bounds without network access. The downloaded `download_ib.py`, `download_alpaca.py`, and `download_databento.py` are equivalent provider-specific Python launchers requiring the installed ATI SDK environment, not dependency-free programs. They can also run in an authorized existing Python environment with `ati-shared-sdk[local-data]` installed.

Keep credentials only in your local environment; never enter them on the webpage, command line, public file, screenshot or issue. The public shell launcher forwards only the documented provider variables to the one-off container. Do not use `sudo` or change data ownership to work around permission failures.

## Three providers
* IB: an already-authenticated TWS/IB Gateway with API historical-data permission. No API key, username or password is entered into this script. Specify `--ib-host`, `--ib-port` and a distinct `--ib-client-id`. `--ib-contracts /path/contracts.json` maps each requested label to the exact `{ "conId": 123, "exchange": "SMART" }`; this is an illustrative number, not a tradable contract recommendation. Set ATI_IB_CONTRACTS_FILE to that local non-secret file; the public shell mounts it read-only into the container. The script only calls API handshake, qualification and historical data methods, never order/account methods. Enable read-only API access in Gateway for additional enforcement; the transport handshake itself is not a read-only authorization boundary. Supported intervals: 1m,5m,15m,1h. Daily IB date/session mapping is deliberately not guessed. Historical entitlement/pacing and expired-future limits still apply.
* Alpaca: configure `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` locally. This path supports US stock/ETF symbols, raw adjustment, `--feed iex|sip` with your actual entitlement, intervals 1m,5m,15m,1h,1d. It does not reinterpret futures symbols as equities. Pagination is exhausted and repeated cursors fail. A paid subscription may be required; the script never upgrades it.
* Databento: configure `DATABENTO_API_KEY` locally, specify `--dataset` and exact raw instrument symbols, intervals 1m,1h,1d. No inferred continuous-futures stitching or implicit contract roll. `--execute --max-cost 5` obtains all quotes before downloading; confirm the quoted purchase in your terminal or explicitly use `--confirm-cost`. The ceiling checks the quote, not a guaranteed invoice cap: Databento bills actual delivered bytes. Declining/over-ceiling never calls timeseries download. No automatic paid retries.

## Native files, safety and interpretation
Default container directory: `/app/data/market-data`; public host directory: `./data/market-data` (or `ATI_MARKET_DATA_DIR`). Normal research mounts it read-only; only the explicit download command mounts it read-write. Files use `bar_<timeframe>/<symbol>/YYYYMM_<symbol>_<timeframe>.parquet`, native UTC timestamp + OHLCV schema, per-month atomic replacement and timestamp deduplication. Never merge different contracts/providers/feed identities. If prior files have unknown provenance or different source evidence, use a separate data directory and explicitly select it for research; existing data is not overwritten or deleted. The source sidecar is import evidence only, never a backtest registration prerequisite.

`start` is inclusive and `end` exclusive, both timezone-qualified. A successful write means observed rows were written, not a promise of continuous/session-complete coverage. Empty data fails; readable earlier chunks remain after a later error. The UI reports actual timestamp bounds and unreadable files rather than inferring whole months from filenames. Local compute avoids corresponding cloud-compute credits, not provider/AI/subscription charges.


Public installation: run `bash scripts/download_market_data.sh` after installing local offline tools. Set keys only in your local shell, never in the webpage. Optional `ATI_IB_CONTRACTS_FILE` names a local exact-contract JSON mounted read-only; Gateway remains user-managed.
