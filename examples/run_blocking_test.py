import asyncio
import sys
import threading
import time
import typing

from httpx_aiohttp_transport import create_aiohttp_backed_httpx_client


def format_thread_stack(thread_id: int, *, limit: int = 100) -> typing.Optional[str]:
    """
    快速格式化指定线程的调用栈，避免读取源代码文件

    Args:
        thread_id: 目标线程ID
        limit: 堆栈深度限制

    Returns:
        str: 格式化后的调用栈字符串，如果线程不存在则返回 None
    """
    # 获取所有线程的当前帧
    frames = sys._current_frames()
    frame = frames.get(thread_id)

    if frame is None:
        return None

    # 构建调用栈信息
    lines = ["Stack trace (most recent call last):\n"]

    # 遍历帧
    depth = 0
    while frame and depth < limit:
        filename = frame.f_code.co_filename
        function = frame.f_code.co_name
        lineno = frame.f_lineno
        lines.append(f'  File "{filename}", line {lineno}, in {function}\n')
        frame = frame.f_back
        depth += 1

    return "".join(reversed(lines))


class EventLoopBlockingDetector:
    def __init__(
        self, eventloop: asyncio.AbstractEventLoop, threshold: float = 0.5
    ) -> None:
        self.threshold = threshold
        self.running = False
        self.eventloop = eventloop
        self._last_tic_time = float("inf")
        # 监控线程引用
        self._monitor_thread: typing.Optional[threading.Thread] = None

        # 用于在事件循环中定期更新的标记
        self._loop_tick = 0
        self._last_tick = 0

    async def _get_main_thread_id(self) -> int:
        return threading.get_ident()

    async def _update_tick(self) -> None:
        """在事件循环中运行的协程，用于更新计数器"""
        while self.running:
            self._loop_tick += 1
            self._last_tic_time = time.monotonic()
            await asyncio.sleep(0.01)  # 很小的睡眠时间

    def _monitor_loop(self) -> None:
        """在单独线程中运行的监控函数"""

        # 记录主线程（运行事件循环的线程）ID
        _eventloop_thread_id: int = asyncio.run_coroutine_threadsafe(
            self._get_main_thread_id(), self.eventloop
        ).result()

        while self.running:
            time.sleep(0.05)  # 监控间隔

            # 检查tick是否更新
            current_tick = self._loop_tick
            if current_tick == self._last_tick:  # 如果tick没有变化
                # 计算阻塞时间
                current_time = time.monotonic()
                blocking_time = current_time - self._last_tic_time
                if blocking_time > self.threshold:
                    print(
                        f"Event loop blocked for {blocking_time:.3f} seconds\n"
                        f"Stack trace:\n{format_thread_stack(_eventloop_thread_id)}"
                    )
            else:
                self._last_tick = current_tick

    def start(self):
        """启动检测器"""
        self.running = True

        # 启动tick更新任务
        asyncio.run_coroutine_threadsafe(self._update_tick(), self.eventloop)

        # 启动监控线程
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def stop(self):
        """停止检测器"""
        self.running = False
        if self._monitor_thread:
            self._monitor_thread.join()


ADDRESS = "https://www.baidu.com"


async def request_with_httpx(client):
    rsp = await client.get(ADDRESS)
    return rsp.text


async def benchmark_httpx_with_aiohttp_transport(n):
    async with create_aiohttp_backed_httpx_client() as client:
        start = time.time()
        tasks = []
        print(await request_with_httpx(client))
        for i in range(n):
            tasks.append(request_with_httpx(client))
        await asyncio.gather(*tasks)
        return time.time() - start


def main():
    eventloop = asyncio.new_event_loop()
    EventLoopBlockingDetector(eventloop=eventloop, threshold=0.001).start()
    eventloop.run_until_complete(benchmark_httpx_with_aiohttp_transport(1024))


if __name__ == "__main__":
    main()
