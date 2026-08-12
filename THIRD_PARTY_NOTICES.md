# Third-Party Notices

This file records third-party projects and services used by this independent repository. The MIT license in [`LICENSE`](LICENSE) applies only to the original code in this repository; it does not relicense third-party components.

## NautilusTrader

- Project: **NautilusTrader**
- Author / maintainer: **Nautech Systems**
- Official source: <https://github.com/nautechsystems/nautilus_trader>
- Official documentation: <https://nautilustrader.io/docs/>
- Package: <https://pypi.org/project/nautilus_trader/>
- Version currently pinned by this repository: `2.0.0rc2`
- License: **GNU Lesser General Public License v3.0 or later (`LGPL-3.0-or-later`)**
- Upstream license text: <https://github.com/nautechsystems/nautilus_trader/blob/develop/LICENSE>

NautilusTrader is installed as a separate, unmodified runtime dependency from its official PyPI distribution and is cryptographically pinned in `uv.lock`. This repository does not vendor or redistribute NautilusTrader source code or binary wheels and is not a fork of the upstream project.

This repository is not affiliated with, maintained by, sponsored by, or endorsed by Nautech Systems or the NautilusTrader project.

## PyBroker

- Project: **PyBroker**
- Official source: <https://github.com/edtechre/pybroker>
- Package: <https://pypi.org/project/lib-pybroker/>
- Version approved for the isolated research pilot: `lib-pybroker==1.2.14` (import name `pybroker`)
- License: **Apache License 2.0 with Commons Clause**
- Upstream license text: <https://github.com/edtechre/pybroker/blob/master/LICENSE>

PyBroker is not a dependency of the current Nautilus runtime. Stage 0 does not install or redistribute it. A later, separately approved stage may use it only in an isolated internal research environment for provisional screening and candidate ranking. This approval does not cover paid hosting, consulting, commercialization, Testnet, or live trading; those uses require separate legal and operational review. PyBroker output is not the authoritative source for portfolio accounting, fees, funding, fills, or trading decisions.

## Binance

This project retrieves public Binance USD-M Futures market data through public HTTP APIs. It does not vendor Binance SDK code, credentials, or downloaded market data. Binance names and trademarks belong to their respective owners. Users are responsible for complying with applicable Binance API terms and local requirements.
