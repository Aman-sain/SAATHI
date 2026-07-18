"""SAATHI hub entry point (§5/§8): config → logging → uvicorn(app factory).

Binds 0.0.0.0 so LAN phones can reach the PWA (§24); the private hotspot is
the trust boundary (§18). Ownership: hub/ root is M1's — this file was drafted
by M3 under D-006 (lead-approved), flagged for Aman's review.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn

from app import config, main

if __name__ == "__main__":
    settings = config.load()
    main.setup_logging(settings)
    uvicorn.run(
        main.create_app(settings),
        host="0.0.0.0",
        port=settings.http_port,
        log_level="warning",
    )
