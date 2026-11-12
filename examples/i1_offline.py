"""Run without credentials or network: python -m examples.i1_offline."""

import asyncio
from dataclasses import asdict
import json

import httpx

from organizer_api import APIConfig, OrganizerClient


async def main() -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        route = request.url.path.rsplit("/", 1)[-1]
        calls.append(route)
        if route == "completions":
            payload = {"choices": [{"message": {"content": "Это синтетический ответ тестового сервера."}, "finish_reason": "stop"}]}
        elif route == "embeddings":
            payload = {"data": [{"index": 1, "embedding": [3, 4]}, {"index": 0, "embedding": [1, 2]}]}
        elif route == "rerank":
            payload = {"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.2}]}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json=payload)

    config = APIConfig(base_url="http://offline.invalid/v1", api_key="synthetic-offline-key",
                       model="synthetic-generator")
    async with OrganizerClient(config, transport=httpx.MockTransport(respond)) as client:
        result = await client.chat([{"role": "user", "content": "Тест клиента."}])
        vectors = await client.embed(["первый текст", "второй текст"])
        ranks = await client.rerank("второй", ["первый текст", "второй текст"])
        print(json.dumps({"mode": "offline_mock", "real_proxy_checked": False,
                          "chat": result.text, "vectors_in_input_order": vectors.vectors,
                          "ranked_indices": [x.index for x in ranks.ranks],
                          "routes": calls, "metrics": [asdict(x) for x in client.metrics]},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
