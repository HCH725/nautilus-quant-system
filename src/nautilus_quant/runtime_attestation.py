"""Root runtime attestation for the Nautilus-only quant system.

The root runtime attests exactly four things: the NautilusTrader version, the
Python version, the shared strategy-family kernel identity, and (via callers)
the canonical data snapshot. There is no isolated second-engine environment
anymore, so there is nothing else to hash.
"""

from __future__ import annotations

from hashlib import sha256
import json
import platform

import nautilus_trader

from .strategy_families import KERNEL_HASH, KERNEL_VERSION


def root_runtime_identity() -> str:
    """Return the deterministic identity of the Nautilus-only root runtime."""
    preimage = {
        "kernel_hash": KERNEL_HASH,
        "kernel_version": KERNEL_VERSION,
        "nautilus_trader": nautilus_trader.__version__,
        "python_version": platform.python_version(),
        "schema_version": "root-runtime-v1",
    }
    payload = (
        json.dumps(
            preimage,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    return sha256(payload).hexdigest()
