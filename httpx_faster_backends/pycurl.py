import asyncio
import io
from contextvars import ContextVar
from types import TracebackType
from typing import Dict, NamedTuple, Optional, Self, Callable

import httpx
import pycurl
from httpx import AsyncBaseTransport


class Handle(NamedTuple):
    future: asyncio.Future
    buffer: io.BytesIO
    headers: Dict[str, str]
    request: httpx.Request

class MultiHandler:
    def __init__(self):
        self.multi = pycurl.CurlMulti()
        self.handles: Dict[pycurl.Curl, Handle] = {}
        self.loop = asyncio.get_event_loop()
        self.multi.setopt(pycurl.M_SOCKETFUNCTION, self._socket_callback)
        self.multi.setopt(pycurl.M_TIMERFUNCTION, self._timer_callback)
        # self._timer_task = self.loop.create_task(self._on_timer())

    def _socket_callback(self, event, fd, multi, data):
        # 使用位运算替代多个if判断
        if event & (pycurl.POLL_IN | pycurl.POLL_OUT):
            if event & pycurl.POLL_IN:
                self.loop.add_reader(fd, self._socket_action, fd, pycurl.CSELECT_IN)
            if event & pycurl.POLL_OUT:
                self.loop.add_writer(fd, self._socket_action, fd, pycurl.CSELECT_OUT)
        elif event == pycurl.POLL_REMOVE:
            self.loop.remove_reader(fd)
            self.loop.remove_writer(fd)

    def _timer_callback(self, timeout_ms):
        # PyCURL要求实现这个函数，即使我们不使用timeout_ms
        pass

    def _socket_action(self, fd, flags):
        ret, _ = self.multi.socket_action(fd, flags)
        self._check_multi_info()

    async def _on_timer(self):
        while True:
            ret, _ = self.multi.socket_action(pycurl.SOCKET_TIMEOUT, 0)
            self._check_multi_info()
            await asyncio.sleep(0)

    def _check_multi_info(self):
        # 批量处理完成的请求
        completed_requests = []
        while True:
            num_q, ok_list, err_list = self.multi.info_read(1024)
            
            for curl in ok_list:
                self._handle_completed(curl)
            for curl, errnum, errmsg in err_list:
                self._handle_failed(curl, errnum, errmsg)
            if num_q == 0:
                break

    def _handle_completed(self, curl: pycurl.Curl):
        handle = self.handles.pop(curl, None)
        if handle and not handle.future.done():
            status_code = curl.getinfo(pycurl.RESPONSE_CODE)
            content = handle.buffer.getvalue()
            response = httpx.Response(
                status_code=status_code,
                headers=handle.headers,
                content=content,
                request=handle.request,
            )
            handle.future.set_result(response)
        self.multi.remove_handle(curl)

    def _handle_failed(self, curl: pycurl.Curl, errnum: int, errmsg: str):
        handle = self.handles.pop(curl, None)
        if handle and not handle.future.done():
            exc = map_pycurl_exception(pycurl.error(errnum, errmsg))
            handle.future.set_exception(exc)
        self.multi.remove_handle(curl)

    async def add_request(self, curl: pycurl.Curl, request: httpx.Request) -> httpx.Response:
        future = self.loop.create_future()
        buffer = io.BytesIO()
        headers = {}

        curl.setopt(pycurl.WRITEDATA, buffer)
        curl.setopt(pycurl.HEADERFUNCTION, lambda header: _parse_header(header, headers))

        self.handles[curl] = Handle(future, buffer, headers, request)
        self.multi.add_handle(curl)

        # 手动触发一次socket_action启动请求
        self.loop.call_soon(self._socket_action, pycurl.SOCKET_TIMEOUT, 0)

        return await future

    async def close(self):
        # 取消并等待timer task完成
        # self._timer_task.cancel()
        # try:
        #     await self._timer_task
        # except asyncio.CancelledError:
        #     pass
    
        # 清理所有remaining handles
        for curl in list(self.handles.keys()):
            handle = self.handles[curl]
            if not handle.future.done():
                handle.future.cancel()
            self.multi.remove_handle(curl)
            curl.close()
        self.handles.clear()
    
        # 清理multi对象
        self.multi.close()


# 辅助函数
def _parse_header(header_line: bytes, headers: dict[str, str]) -> None:
    header_line_str = header_line.decode("iso-8859-1")
    if ":" not in header_line_str:
        return
    name, value = header_line_str.split(":", 1)
    headers[name.strip().lower()] = value.strip()

def map_pycurl_exception(exc: pycurl.error) -> Exception:
    error_code, error_msg = exc.args
    PYCURL_TO_HTTPX_EXCEPTIONS = {
        pycurl.E_COULDNT_RESOLVE_PROXY: httpx.ProxyError,
        pycurl.E_COULDNT_RESOLVE_HOST: httpx.ConnectError,
        pycurl.E_COULDNT_CONNECT: httpx.ConnectError,
        pycurl.E_OPERATION_TIMEDOUT: httpx.TimeoutException,
        pycurl.E_SEND_ERROR: httpx.WriteError,
        pycurl.E_RECV_ERROR: httpx.ReadError,
        pycurl.E_SSL_CONNECT_ERROR: httpx.ProtocolError,
        pycurl.E_TOO_MANY_REDIRECTS: httpx.TooManyRedirects,
        pycurl.E_URL_MALFORMAT: httpx.InvalidURL,
    }
    httpx_exc = PYCURL_TO_HTTPX_EXCEPTIONS.get(error_code, httpx.HTTPError)
    return httpx_exc(f"PyCURL error ({error_code}): {error_msg}")

class PyCURLTransport(AsyncBaseTransport):
    """基于PyCURL的HTTPX异步传输层"""

    def __init__(
            self,
            *,
            max_workers: int = 10,
            max_connections_per_host: int = 0,
            max_connections: int = 1000,
            verify_ssl: bool = True,
            timeout: Optional[float] = None,
            proxy: Optional[str] = None,
            cookies_enabled: bool = True,
            follow_redirects: bool = True,
            max_redirects: int = 10,
            interface: Optional[str] = None,
            default_headers: Optional[Dict[str, str]] = None,
            max_idle_time: float = 60,
            cleanup_interval: float = 30
    ):
        self.base_curl = pycurl.Curl()
        self.base_curl.setopt(pycurl.SSL_VERIFYPEER, 1 if verify_ssl else 0)
        self.base_curl.setopt(pycurl.SSL_VERIFYHOST, 2 if verify_ssl else 0)
        self.base_curl.setopt(pycurl.PROXY, proxy) if proxy else None
        self.base_curl.setopt(pycurl.INTERFACE, interface) if interface else None
        self.base_curl.setopt(pycurl.FOLLOWLOCATION, 1 if follow_redirects else 0)
        self.base_curl.setopt(pycurl.MAXREDIRS, max_redirects if follow_redirects else 0)
        self.base_curl.setopt(pycurl.TCP_KEEPALIVE, 1)
        self.base_curl.setopt(pycurl.TCP_KEEPIDLE, 120)
        self.base_curl.setopt(pycurl.TCP_KEEPINTVL, 60)
        self.base_curl.setopt(pycurl.FRESH_CONNECT, 0)
        self.base_curl.setopt(pycurl.FORBID_REUSE, 0)
        self.base_curl.setopt(pycurl.MAXCONNECTS, max_connections)
        self.base_curl.setopt(pycurl.CONNECTTIMEOUT, timeout) if timeout else None

        self._multi_handler = MultiHandler()

    def _prepare_curl_for_request(
            self, request: httpx.Request
    ) -> pycurl.Curl:
        curl = self.base_curl.duphandle()

        # 设置URL
        curl.setopt(pycurl.URL, str(request.url))

        # 设置HTTP方法
        if request.method == "HEAD":
            curl.setopt(pycurl.NOBODY, 1)
        elif request.method != "GET":
            curl.setopt(pycurl.CUSTOMREQUEST, request.method)

        # 设置请求体
        if request.content:
            curl.setopt(pycurl.POSTFIELDS, request.content)

        # 设置请求头
        headers = []
        for name, value in request.headers.items():
            headers.append(f"{name}: {value}")
        curl.setopt(pycurl.HTTPHEADER, headers)
        return curl

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
            self,
            exc_type: Optional[type[BaseException]] = None,
            exc_value: Optional[BaseException] = None,
            traceback: Optional[TracebackType] = None,
    ) -> None:
        await self.aclose()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """处理异步HTTP请求"""
        if (_rsp := try_to_get_mocked_response(request)) is not None:
            return _rsp

        try:
            response = await self._multi_handler.add_request(
                self._prepare_curl_for_request(request), request
            )
            return response
        except pycurl.error as e:
            raise map_pycurl_exception(e) from e
        except Exception as e:
            if isinstance(e, asyncio.TimeoutError):
                raise httpx.TimeoutException(str(e))
            raise httpx.HTTPError(f"Unknown error: {str(e)}")

    async def aclose(self) -> None:
        """关闭传输层并清理资源"""
        await self._multi_handler.close()


# 响应模拟支持，与aiohttp版本兼容
mock_router: ContextVar[Callable[[httpx.Request], httpx.Response]] = ContextVar(
    "mock_router"
)


def try_to_get_mocked_response(
    request: httpx.Request,
) -> Optional[httpx.Response]:
    try:
        _mock_handler = mock_router.get()
    except LookupError:
        return None
    return _mock_handler(request)


def create_pycurl_backed_httpx_client(
    *,
    headers: Optional[Dict[str, str]] = None,
    total_timeout: Optional[float] = None,
    base_url: str = "",
    proxy: Optional[str] = None,
    max_workers: int = 10,
    verify_ssl: bool = True,
    cookies_enabled: bool = False,
    follow_redirects: bool = True,
    max_redirects: int = 10,
    interface: Optional[str] = None,
        max_connections: int = 100,
        max_connections_per_host: int = 0,
) -> httpx.AsyncClient:
    """
    创建基于PyCURL的HTTPX异步客户端

    Args:
        headers: 默认请求头
        total_timeout: 请求超时时间（秒）
        base_url: 基础URL
        proxy: 代理服务器地址
        max_workers: 线程池最大工作线程数
        verify_ssl: 是否验证SSL证书
        cookies_enabled: 是否启用cookie
        follow_redirects: 是否跟随重定向
        max_redirects: 最大重定向次数
        interface: 指定网络接口

    Returns:
        基于PyCURL的HTTPX异步客户端
    """
    default_headers = headers or {}

    return httpx.AsyncClient(
        base_url=base_url,
        headers=default_headers,
        verify=verify_ssl,
        transport=PyCURLTransport(
            max_workers=max_workers,
            verify_ssl=verify_ssl,
            timeout=total_timeout,
            proxy=proxy,
            cookies_enabled=cookies_enabled,
            follow_redirects=follow_redirects,
            max_redirects=max_redirects,
            interface=interface,
            default_headers=default_headers,
            max_connections=max_connections,
            max_connections_per_host=max_connections_per_host,
        ),
    )


__all__ = [
    "PyCURLTransport",
    "create_pycurl_backed_httpx_client",
    "mock_router",
]
