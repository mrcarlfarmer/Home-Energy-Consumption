"""External HTTP workload; does not count the load generator as container memory."""

import argparse
import asyncio
import json
import ssl
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx


async def run(
    url: str,
    certificate: Path,
    password_file: Path,
    repeats: int,
    require_ingestion: bool,
) -> None:
    context = ssl.create_default_context(cafile=str(certificate))
    async with httpx.AsyncClient(
        base_url=url,
        verify=context,
        timeout=30,
        trust_env=False,
        headers={"Origin": url},
        limits=httpx.Limits(max_connections=10),
    ) as client:
        password = await asyncio.to_thread(password_file.read_text)
        login = await client.post("/api/auth/login", json={"password": password.strip()})
        login.raise_for_status()
        events = [asyncio.Event() for _ in range(4)]

        async def stream(index: int) -> None:
            async with client.stream("GET", "/api/telemetry/live") as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        events[index].set()

        tasks = [asyncio.create_task(stream(i)) for i in range(4)]
        try:
            async with asyncio.timeout(20):
                await asyncio.gather(*(event.wait() for event in events))
            results: dict[str, object] = {}
            versions: set[int] = set()
            for days in (1, 30, 365):
                for endpoint in ("summary", "history"):
                    elapsed = []
                    for _ in range(repeats):
                        end = datetime.now(UTC).replace(second=0, microsecond=0)
                        query = {
                            "from": (end - timedelta(days=days)).isoformat(),
                            "to": end.isoformat(),
                        }
                        start = time.perf_counter()
                        response = await client.get(f"/api/analytics/{endpoint}", params=query)
                        if response.status_code != 200:
                            raise RuntimeError(
                                f"{days}d {endpoint}: {response.status_code} {response.text}"
                            )
                        data = response.json()
                        versions.add(data["data_version"])
                        if endpoint == "history":
                            lengths = {len(values) for values in data["series"].values()}
                            assert len(lengths) == 1 and next(iter(lengths)) <= 1500
                        else:
                            assert len(data["thresholds"]) == 3
                            assert data["sampled_peak_w"] == 6200
                            assert 0 < data["coverage_pct"] <= 100
                        elapsed.append(time.perf_counter() - start)
                    results[f"{days}d_{endpoint}"] = {
                        "seconds": elapsed,
                        "median": statistics.median(elapsed),
                        "max": max(elapsed),
                    }
            if require_ingestion:
                deadline = time.monotonic() + 60
                while len(versions) < 2 and time.monotonic() < deadline:
                    response = await client.get("/api/analytics/summary", params=query)
                    response.raise_for_status()
                    versions.add(response.json()["data_version"])
                    await asyncio.sleep(1)
                if len(versions) < 2:
                    raise AssertionError("No concurrent ingestion observed; enable the collector")
            results["observed_data_versions"] = sorted(versions)
            print(json.dumps(results, indent=2))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://localhost:18443")
    parser.add_argument("--certificate", type=Path, required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--require-ingestion", action="store_true")
    args = parser.parse_args()
    asyncio.run(
        run(args.url, args.certificate, args.password_file, args.repeats, args.require_ingestion)
    )
