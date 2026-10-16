"""Run the simulated TMS as its own process: python -m tms_sim [--check]

Env: TMS_TOKEN (required), TMS_PORT (default 9100), TMS_FAULT_RATE (0-1, default 0).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from app.logging_setup import configure_logging, log_event
from app.tms.client import TmsClient
from tms_sim.server import TmsSimulator

log = logging.getLogger("clf.tms_sim")


async def _serve(token: str, port: int, fault_rate: float) -> None:
    simulator = TmsSimulator(token, fault_rate=fault_rate)
    server = await simulator.start("0.0.0.0", port)
    log_event(log, "tms_sim_started", port=port, fault_rate=fault_rate, loads=len(simulator.loads))
    async with server:
        await server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulated Crystal Lane legacy TMS (TCP)")
    parser.add_argument("--check", action="store_true", help="exit 0 if the local simulator answers PING")
    args = parser.parse_args()

    token = os.environ.get("TMS_TOKEN") or ""
    port = int(os.environ.get("TMS_PORT") or 9100)
    if args.check:
        return 0 if asyncio.run(TmsClient("127.0.0.1", port, token).ping()) else 1
    if not token:
        print("TMS_TOKEN is required", file=sys.stderr)
        return 2
    fault_rate = float(os.environ.get("TMS_FAULT_RATE") or 0)
    configure_logging()
    asyncio.run(_serve(token, port, fault_rate))
    return 0


if __name__ == "__main__":
    sys.exit(main())
