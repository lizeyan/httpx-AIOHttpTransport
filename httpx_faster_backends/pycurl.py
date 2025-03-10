import asyncio
import io
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from types import TracebackType
from typing import Optional, Dict, Callable

import httpx
import pycurl
import typing_extensions as typing
from httpx import AsyncBaseTransport, AsyncByteStream

_executor = ThreadPoolExecutor(max_workers=1024)

# PyCURL到HTTPX异常映射
PYCURL_TO_HTTPX_EXCEPTIONS: Dict[int, type[Exception]] = {
    # DNS相关错误
    pycurl.E_COULDNT_RESOLVE_PROXY: httpx.ProxyError,
    pycurl.E_COULDNT_RESOLVE_HOST: httpx.ConnectError,
    # 连接错误
    pycurl.E_COULDNT_CONNECT: httpx.ConnectError,
    pycurl.E_OPERATION_TIMEDOUT: httpx.TimeoutException,
    pycurl.E_SEND_ERROR: httpx.WriteError,
    pycurl.E_RECV_ERROR: httpx.ReadError,
    # SSL错误
    pycurl.E_SSL_CONNECT_ERROR: httpx.ProtocolError,
    pycurl.E_SSL_CERTPROBLEM: httpx.ProtocolError,
    pycurl.E_SSL_CIPHER: httpx.ProtocolError,
    pycurl.E_PEER_FAILED_VERIFICATION: httpx.ProtocolError,
    # 重定向错误
    pycurl.E_TOO_MANY_REDIRECTS: httpx.TooManyRedirects,
    # URL错误
    pycurl.E_URL_MALFORMAT: httpx.InvalidURL,
    # 其它错误
    pycurl.E_ABORTED_BY_CALLBACK: httpx.RequestError,
}


def map_pycurl_exception(exc: pycurl.error) -> Exception:
    """
    将PyCURL异常映射为对应的HTTPX异常

    Args:
        exc: PyCURL异常实例

    Returns:
        对应的HTTPX异常实例
    """
    error_code, error_msg = exc.args

    # 查找映射的异常类型
    httpx_exc = PYCURL_TO_HTTPX_EXCEPTIONS.get(error_code, httpx.HTTPError)
    return httpx_exc(f"PyCURL error ({error_code}): {error_msg}")


class PyCURLResponseStream(AsyncByteStream):
    """处理PyCURL响应内容的异步流"""

    def __init__(self, response_data: bytes) -> None:
        self.response_data = response_data
        self.stream = io.BytesIO(response_data)
        self.chunk_size = 65536  # 64KB chunks

    async def __aiter__(self) -> typing.AsyncIterator[bytes]:
        # 已经有全部数据，按块返回
        self.stream.seek(0)
        while True:
            chunk = self.stream.read(self.chunk_size)
            if not chunk:
                break
            yield chunk

    async def aclose(self) -> None:
        # 关闭流
        self.stream.close()


def _parse_header(header_line: bytes, headers: dict[str, str]) -> None:
    # HTTP standard specifies that headers are encoded in iso-8859-1.
    # On Python 2, decoding step can be skipped.
    # On Python 3, decoding step is required.
    header_line_str = header_line.decode("iso-8859-1")

    # Header lines include the first status line (HTTP/1.x ...).
    # We are going to ignore all lines that don't have a colon in them.
    # This will botch headers that are split on multiple lines...
    if ":" not in header_line_str:
        return

    # Break the header line into header name and value.
    name, value = header_line_str.split(":", 1)

    # Remove whitespace that may be present.
    # Header lines include the trailing newline, and there may be whitespace
    # around the colon.
    name = name.strip()
    value = value.strip()

    # Header names are case insensitive.
    # Lowercase name here.
    name = name.lower()

    # Now we can actually record the header name and value.
    # Note: this only works when headers are not duplicated, see below.
    headers[name] = value


class PyCURLTransport(AsyncBaseTransport):
    """基于PyCURL的HTTPX异步传输层"""

    __slots__ = (
        "_thread_pool",
        "_max_workers",
        "_verify_ssl",
        "_closed",
        "_timeout",
        "_proxy",
        "_excluded_response_headers",
        "_interface",
        "_follow_redirects",
        "_max_redirects",
        "_cookies_enabled",
        "_default_headers",
    )

    def __init__(
        self,
        *,
        max_workers: int = 10,
        verify_ssl: bool = True,
        timeout: Optional[float] = None,
        proxy: Optional[str] = None,
        cookies_enabled: bool = True,
        follow_redirects: bool = True,
        max_redirects: int = 10,
        interface: Optional[str] = None,
        default_headers: Optional[Dict[str, str]] = None,
    ):
        self._max_workers = max_workers
        self._verify_ssl = verify_ssl
        self._closed = False
        self._timeout = timeout
        self._proxy = proxy
        self._interface = interface
        self._follow_redirects = follow_redirects
        self._max_redirects = max_redirects
        self._cookies_enabled = cookies_enabled
        self._default_headers = default_headers or {}

        # 排除的响应头
        self._excluded_response_headers = {"content-encoding"}
        if not cookies_enabled:
            self._excluded_response_headers.add("set-cookie")

    async def __aenter__(self) -> typing.Self:
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

        # 检查是否有模拟响应
        if (_rsp := try_to_get_mocked_response(request)) is not None:
            return _rsp

        if self._closed:
            raise RuntimeError("Transport is closed")

        try:
            # 使用线程池运行PyCURL请求
            response_data = await asyncio.get_running_loop().run_in_executor(
                _executor, self._perform_request, request
            )
            return response_data
        except pycurl.error as e:
            # 将PyCURL异常映射为HTTPX异常
            raise map_pycurl_exception(e) from e
        except Exception as e:
            # 处理其他异常
            if isinstance(e, asyncio.TimeoutError):
                raise httpx.TimeoutException(str(e))
            raise httpx.HTTPError(f"Unknown error: {str(e)}")

    def _perform_request(self, request: httpx.Request) -> httpx.Response:
        """执行PyCURL请求并返回响应"""

        # 创建cURL对象
        curl = pycurl.Curl()
        # curl.setopt(curl.VERBOSE, True)

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

        # 设置代理
        if self._proxy:
            curl.setopt(pycurl.PROXY, self._proxy)

        # 设置SSL验证
        curl.setopt(pycurl.SSL_VERIFYPEER, 1 if self._verify_ssl else 0)
        curl.setopt(pycurl.SSL_VERIFYHOST, 2 if self._verify_ssl else 0)

        # 设置重定向
        curl.setopt(pycurl.FOLLOWLOCATION, 1 if self._follow_redirects else 0)
        if self._follow_redirects:
            curl.setopt(pycurl.MAXREDIRS, self._max_redirects)

        # 设置超时
        if self._timeout:
            curl.setopt(pycurl.TIMEOUT_MS, int(self._timeout * 1000))

        # 设置接口（如果指定）
        if self._interface:
            curl.setopt(pycurl.INTERFACE, self._interface)

        # 创建响应头和响应体的缓冲区
        response_headers: dict[str, str] = {}
        curl.setopt(
            pycurl.HEADERFUNCTION,
            lambda header: _parse_header(header, response_headers),
        )

        body_buffer = io.BytesIO()
        curl.setopt(pycurl.WRITEDATA, body_buffer)

        # 执行请求
        curl.perform()

        # 获取响应状态码
        status_code = curl.getinfo(pycurl.RESPONSE_CODE)

        # 清理资源
        curl.close()

        # 获取响应体
        body_buffer.seek(0)
        content = body_buffer.getvalue()

        # 创建响应流
        content_stream = PyCURLResponseStream(content)

        # 构建并返回响应
        return httpx.Response(
            status_code=status_code,
            headers=response_headers,
            content=content_stream,
            request=request,
        )

    async def aclose(self) -> None:
        """关闭传输层并清理资源"""
        if not self._closed:
            self._closed = True


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
        ),
    )


__all__ = [
    "PyCURLTransport",
    "create_pycurl_backed_httpx_client",
    "mock_router",
]
