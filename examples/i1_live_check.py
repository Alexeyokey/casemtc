"""Prepared for organizer access; never run implicitly or during unit tests."""

import argparse
import asyncio
from dataclasses import asdict
import json

from organizer_api import APIConfig, ConfigurationError, OrganizerAPIError, OrganizerClient


async def check() -> int:
    try:
        config = APIConfig.from_env()
    except ConfigurationError as error:
        print(json.dumps({"ok": False, "stage": "configuration", "error": str(error)}))
        return 2
    async with OrganizerClient(config) as client:
        probes = (
            ("chat", lambda: client.chat([{"role": "user", "content": "Ответь кратко по-русски: проверка связи."}], max_tokens=64)),
            ("embeddings", lambda: client.embed(["проверка связи", "учёт ресурсов"])),
            ("rerank", lambda: client.rerank("учёт ресурсов", ["синтетический текст об учёте ресурсов", "синтетический текст о погоде"])),
        )
        for operation, probe in probes:
            try:
                await probe()
            except OrganizerAPIError as error:
                print(json.dumps({"ok": False, "operation": operation, "error": str(error),
                                  "metrics": [asdict(x) for x in client.metrics]}))
                return 1
        print(json.dumps({"ok": True, "mode": "live_contract_check",
                          "metrics": [asdict(x) for x in client.metrics]}))
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the organizer proxy with synthetic texts only.")
    parser.add_argument("--live", action="store_true", help="Explicitly allow HTTP requests to the configured organizer proxy")
    args = parser.parse_args()
    if not args.live:
        print("No requests sent. Use --live only after receiving organizer access; see docs/I1_API_CLIENT.md.")
        return 0
    return asyncio.run(check())


if __name__ == "__main__":
    raise SystemExit(main())
