import asyncio
import statistics
import time

import httpx
from aiohttp import ClientSession

from httpx_aiohttp_transport import create_aiohttp_backed_httpx_client

ADDRESS = "https://www.baidu.com"


async def request_with_aiohttp(session):
    async with session.get(ADDRESS) as rsp:
        return await rsp.text()


async def request_with_httpx(client):
    rsp = await client.get(ADDRESS)
    return rsp.text


# 性能测试函数
async def benchmark_aiohttp(n):
    async with ClientSession() as session:
        # make sure code is right
        print(await request_with_aiohttp(session))
        start = time.time()
        tasks = []
        for i in range(n):
            tasks.append(request_with_aiohttp(session))
        await asyncio.gather(*tasks)
        return time.time() - start


async def benchmark_httpx(n):
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(
            timeout=10,
        ),
    ) as client:
        # make sure code is right
        print(await request_with_httpx(client))

        start = time.time()
        tasks = []
        for i in range(n):
            tasks.append(request_with_httpx(client))
        await asyncio.gather(*tasks)
        return time.time() - start


async def benchmark_httpx_with_aiohttp_transport(n):
    async with create_aiohttp_backed_httpx_client() as client:
        start = time.time()
        tasks = []
        for i in range(n):
            tasks.append(request_with_httpx(client))
        await asyncio.gather(*tasks)
        return time.time() - start


async def run_benchmark(requests=1000, rounds=10):
    aiohttp_times = []
    httpx_times = []
    httpx_aio_times = []

    print(f"开始测试 {requests} 并发请求...")

    for i in range(rounds):
        print(f"\n第 {i + 1} 轮测试:")

        # aiohttp 测试
        aiohttp_time = await benchmark_aiohttp(requests)
        aiohttp_times.append(aiohttp_time)
        print(f"aiohttp 耗时: {aiohttp_time:.2f} 秒")

        # 短暂暂停让系统冷却
        await asyncio.sleep(1)

        # httpx 测试
        httpx_time = await benchmark_httpx(requests)
        httpx_times.append(httpx_time)
        print(f"httpx 耗时: {httpx_time:.2f} 秒")

        # 短暂暂停让系统冷却
        await asyncio.sleep(1)

        # httpx 测试
        httpx_time = await benchmark_httpx_with_aiohttp_transport(requests)
        httpx_aio_times.append(httpx_time)
        print(f"httpx (aiohttp transport) 耗时: {httpx_time:.2f} 秒")

    print("\n测试结果汇总:")
    print(f"aiohttp 平均耗时: {statistics.mean(aiohttp_times):.2f} 秒")
    print(f"httpx 平均耗时: {statistics.mean(httpx_times):.2f} 秒")
    print(
        f"httpx (aiohttp transport) 平均耗时: {statistics.mean(httpx_aio_times):.2f} 秒"
    )


if __name__ == "__main__":
    # 运行基准测试
    asyncio.run(run_benchmark(1024))
