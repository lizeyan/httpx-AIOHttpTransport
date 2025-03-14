import os
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional, Dict
from urllib.parse import urlparse

import httpx
import pycurl


@dataclass
class PooledCurl:
    """连接池中的Curl对象封装"""
    curl: pycurl.Curl
    host: str  # 连接对应的host
    port: int  # 连接对应的port
    last_used: float  # 最后使用时间
    in_use: bool = False  # 是否在使用中

class HostConnectionPool:
    """针对单个host:port的连接池"""

    def __init__(self, parent_pool: 'CurlConnectionPool', max_connections: int = 10):
        self.max_connections = max_connections
        self.connections: list[PooledCurl] = []
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.parent_pool = parent_pool  # 引用父连接池

    def _create_connection(self, base_curl: pycurl.Curl, host: str, port: int) -> PooledCurl:
        """创建新的连接"""
        curl = base_curl.duphandle()
        return PooledCurl(
            curl=curl,
            host=host,
            port=port,
            last_used=time.time()
        )

    def get_connection(self, base_curl: pycurl.Curl, host: str, port: int) -> PooledCurl:
        """获取一个可用连接"""
        with self.condition:
            while True:
                # 首先尝试复用空闲连接
                for conn in self.connections:
                    if not conn.in_use:
                        conn.in_use = True
                        conn.last_used = time.time()
                        return conn

                # 检查是否可以创建新连接
                if self.max_connections > 0 and len(self.connections) < self.max_connections:
                    # 请求父池分配新连接额度
                    if self.parent_pool.try_allocate_connection():
                        try:
                            conn = self._create_connection(base_curl, host, port)
                            conn.in_use = True
                            self.connections.append(conn)
                            return conn
                        except Exception:
                            # 如果创建失败，释放额度
                            self.parent_pool.release_connection()
                            raise

                # 等待其他线程释放连接
                if not self.condition.wait(timeout=30):  # 30秒超时
                    raise httpx.PoolTimeout("No available connections after 30 seconds")

    def return_connection(self, conn: PooledCurl) -> None:
        """归还连接到连接池"""
        with self.condition:
            if conn in self.connections:  # 确保连接属于此池
                conn.in_use = False
                conn.last_used = time.time()
                self.condition.notify()

    def cleanup_old_connections(self, max_idle_time: float = 60) -> None:
        """清理超过最大空闲时间的连接"""
        current_time = time.time()
        with self.lock:
            old_count = len(self.connections)
            self.connections = [
                conn for conn in self.connections
                if conn.in_use or (current_time - conn.last_used) <= max_idle_time
            ]
            # 更新父池的总连接计数
            removed_count = old_count - len(self.connections)
            if removed_count > 0:
                self.parent_pool.release_connections(removed_count)

    def close(self) -> None:
        """关闭所有连接"""
        with self.lock:
            count = len(self.connections)
            for conn in self.connections:
                conn.curl.close()
            self.connections.clear()
            # 更新父池的总连接计数
            self.parent_pool.release_connections(count)


@lru_cache(maxsize=int(os.getenv("HTTP_URLPARSE_CACHE_SIZE", 1024)))
def _get_pool_key(url: str) -> str:
    """获取连接池键值(host:port)"""
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    return f"{parsed.hostname}:{port}"


class CurlConnectionPool:
    """基于host的Curl连接池管理器"""

    def __init__(
            self,
            max_total_connections: int = 100,  # 新增：总连接数限制
            max_connections_per_host: int = 10,
            max_idle_time: float = 60,
            cleanup_interval: float = 30
    ):
        self.max_total_connections = max_total_connections
        self.max_connections_per_host = max_connections_per_host
        self.max_idle_time = max_idle_time
        self.cleanup_interval = cleanup_interval

        self._base_curl: Optional[pycurl.Curl] = None
        self._host_pools: Dict[str, HostConnectionPool] = {}
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)  # 新增：用于总连接数同步
        self._last_cleanup = time.time()
        self._total_connections = 0  # 新增：当前总连接数

    def try_allocate_connection(self) -> bool:
        """尝试分配一个连接额度"""
        with self._condition:
            if self._total_connections >= self.max_total_connections:
                return False
            self._total_connections += 1
            return True

    def release_connection(self) -> None:
        """释放一个连接额度"""
        self.release_connections(1)

    def release_connections(self, count: int) -> None:
        """释放多个连接额度"""
        with self._condition:
            self._total_connections = max(0, self._total_connections - count)
            self._condition.notify_all()

    def _get_host_pool(self, pool_key: str) -> HostConnectionPool:
        """获取或创建host连接池"""
        with self._lock:
            if pool_key not in self._host_pools:
                self._host_pools[pool_key] = HostConnectionPool(
                    parent_pool=self,
                    max_connections=self.max_total_connections,
                )
            return self._host_pools[pool_key]

    def _cleanup_old_connections(self) -> None:
        """定期清理空闲连接"""
        current_time = time.time()
        if current_time - self._last_cleanup >= self.cleanup_interval:
            with self._lock:
                for pool in self._host_pools.values():
                    pool.cleanup_old_connections(self.max_idle_time)
                self._last_cleanup = current_time

    def initialize(self, **options) -> None:
        """初始化连接池的基础curl配置"""
        with self._lock:
            if self._base_curl is None:
                curl = pycurl.Curl()
                # 设置基础选项
                for key, value in options.items():
                    # 将字符串选项名转换为pycurl常量
                    curl_option = getattr(pycurl, key)
                    curl.setopt(curl_option, value)
                self._base_curl = curl

    def get_curl(self, url: str) -> PooledCurl:
        """获取用于特定URL的curl句柄"""
        if not self._base_curl:
            raise RuntimeError("Connection pool not initialized")

        self._cleanup_old_connections()

        pool_key = _get_pool_key(url)
        host_pool = self._get_host_pool(pool_key)

        parsed = urlparse(url)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        return host_pool.get_connection(self._base_curl, parsed.hostname, port)

    def return_curl(self, pooled_curl: PooledCurl) -> None:
        """归还curl句柄到连接池"""
        pool_key = f"{pooled_curl.host}:{pooled_curl.port}"
        if pool_key in self._host_pools:
            self._host_pools[pool_key].return_connection(pooled_curl)

    def close(self) -> None:
        """关闭连接池"""
        with self._lock:
            if self._base_curl:
                self._base_curl.close()
                self._base_curl = None

            for pool in self._host_pools.values():
                pool.close()
            self._host_pools.clear()