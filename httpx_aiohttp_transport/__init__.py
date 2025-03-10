import asyncio
from contextvars import ContextVar
from types import TracebackType

import aiohttp
import httpx
import httpx._urlparse
import httpx._urls
import typing_extensions as typing
from httpx import AsyncBaseTransport, AsyncByteStream
from yarl import URL

AIOHTTP_TO_HTTPX_EXCEPTIONS: dict[type[Exception], type[Exception]] = {
    # Order matters here, most specific exception first
    # DNS相关异常
    aiohttp.ClientConnectorDNSError: httpx.ConnectError,
    # 代理相关异常
    aiohttp.ClientProxyConnectionError: httpx.ProxyError,
    # SSL相关异常
    aiohttp.ClientConnectorCertificateError: httpx.ProtocolError,
    aiohttp.ClientSSLError: httpx.ProtocolError,
    aiohttp.ServerFingerprintMismatch: httpx.ProtocolError,
    # 网络相关异常
    aiohttp.ClientConnectionResetError: httpx.ConnectError,
    aiohttp.ClientConnectorError: httpx.ConnectError,
    aiohttp.ClientOSError: httpx.ConnectError,
    # 连接断开异常
    aiohttp.ServerDisconnectedError: httpx.ReadError,
    # 响应相关异常
    aiohttp.ClientConnectionError: httpx.NetworkError,
    aiohttp.ClientPayloadError: httpx.ReadError,
    aiohttp.ContentTypeError: httpx.ReadError,
    aiohttp.TooManyRedirects: httpx.TooManyRedirects,
    # URL相关异常
    aiohttp.InvalidURL: httpx.InvalidURL,
    # 基础异常
    aiohttp.ClientError: httpx.RequestError,
}


def map_aiohttp_exception(exc: Exception) -> Exception:
    """
    将 aiohttp 异常映射为对应的 httpx 异常

    Args:
        exc: aiohttp 异常实例

    Returns:
        对应的 httpx 异常实例
    """
    for aiohttp_exc, httpx_exc in AIOHTTP_TO_HTTPX_EXCEPTIONS.items():
        if isinstance(exc, aiohttp_exc):
            return httpx_exc(str(exc))

    # 处理 asyncio 的超时异常
    if isinstance(exc, asyncio.TimeoutError):
        return httpx.TimeoutException(str(exc))

    # 未知异常，包装为通用 HTTPError
    return httpx.HTTPError(f"Unknown error: {str(exc)}")


class AiohttpResponseStream(AsyncByteStream):
    CHUNK_SIZE = 1024

    def __init__(self, aiohttp_response: aiohttp.ClientResponse) -> None:
        self._aiohttp_response = aiohttp_response

    async def __aiter__(self) -> typing.AsyncIterator[bytes]:
        async for chunk in self._aiohttp_response.content.iter_chunked(self.CHUNK_SIZE):
            yield chunk

    async def aclose(self) -> None:
        await self._aiohttp_response.__aexit__(None, None, None)


SKIP_AUTO_HEADERS = frozenset(
    {
        "content-encoding",
        "accept-encoding",
        "user-agent",
        "connection",
        "deflate",
        "accept",
    }
)


class AiohttpTransport(AsyncBaseTransport):
    __slots__ = (
        "_session",
        "_closed",
        "_no_cookie",
        "_verify_ssl",
        "_excluded_response_headers",
        "_ssl_context",
    )

    def __init__(
        self,
        session: typing.Optional[aiohttp.ClientSession] = None,
        *,
        no_cookie: bool = True,
        verify_ssl: bool = False,
    ):
        self._session = session or aiohttp.ClientSession()
        self._closed = False
        self._no_cookie = no_cookie
        self._verify_ssl = verify_ssl

        self._excluded_response_headers = {"content-encoding"}
        if self._no_cookie:
            self._excluded_response_headers.add("set-cookie")

    async def __aenter__(self) -> typing.Self:
        await self._session.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[type[BaseException]] = None,
        exc_value: typing.Optional[BaseException] = None,
        traceback: typing.Optional[TracebackType] = None,
    ):
        await self._session.__aexit__(exc_type, exc_value, traceback)
        self._closed = True

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (
            _rsp := try_to_get_mocked_response(request)
        ) is not None:  # 为了兼容RESPX mock
            return _rsp

        if self._closed:
            raise RuntimeError("Transport is closed")

        try:
            # 准备请求参数
            url = URL.build(
                scheme=request.url.scheme,
                user=request.url.username or None,
                password=request.url.password or None,
                host=request.url.host,
                port=request.url.port,
                path=request.url.path,
                query_string=request.url.query.decode("utf-8"),
                fragment=request.url.fragment,
            )

            content = request.content

            response = await self._session.request(
                method=request.method,
                url=url,
                headers=request.headers,
                data=content,
                allow_redirects=True,
                skip_auto_headers=SKIP_AUTO_HEADERS,
                ssl=self._verify_ssl,
            ).__aenter__()

            content_stream = AiohttpResponseStream(response)

            # 转换headers
            response_headers = [
                (k, v)
                for k, v in [(k.lower(), v) for k, v in response.headers.items()]
                if k not in self._excluded_response_headers
            ]

            # 构建httpx.Response
            return httpx.Response(
                status_code=response.status,
                headers=response_headers,
                content=content_stream,
                request=request,
            )
        except Exception as e:
            raise map_aiohttp_exception(e) from e

    async def aclose(self):
        if not self._closed:
            self._closed = True
            await self._session.close()


mock_router: ContextVar[typing.Callable[[httpx.Request], httpx.Response]] = ContextVar(
    "mock_router"
)


def try_to_get_mocked_response(
    request: httpx.Request,
) -> typing.Optional[httpx.Response]:
    try:
        _mock_handler = mock_router.get()
    except LookupError:
        return None
    return _mock_handler(request)


def create_aiohttp_backed_httpx_client(
    *,
    headers: typing.Optional[dict[str, str]] = None,
    total_timeout: typing.Optional[float] = None,
    base_url: str = "",
    proxy: typing.Optional[str] = None,
    keepalive_timeout: float = 15,
    max_connections: int = 100,
    max_connections_per_host: int = 0,
    verify_ssl: bool = False,
    login: typing.Optional[str] = None,
    password: typing.Optional[str] = None,
    encoding: str = "latin1",
    force_close: bool = False,
    no_cookie: bool = True,
) -> httpx.AsyncClient:
    timeout = aiohttp.ClientTimeout(total=total_timeout)
    connector = aiohttp.TCPConnector(
        keepalive_timeout=keepalive_timeout if not force_close else None,
        limit=max_connections,
        limit_per_host=max_connections_per_host,
        verify_ssl=verify_ssl,
        enable_cleanup_closed=True,
        force_close=force_close,
        ttl_dns_cache=None,
    )
    if login and password:
        auth = aiohttp.BasicAuth(login=login, password=password, encoding=encoding)
    else:
        auth = None
    return httpx.AsyncClient(
        base_url=base_url,
        verify=verify_ssl,
        transport=AiohttpTransport(
            session=aiohttp.ClientSession(
                proxy=proxy,
                auth=auth,
                timeout=timeout,
                connector=connector,
                headers=headers,
                cookie_jar=aiohttp.DummyCookieJar() if no_cookie else None,
            ),
            no_cookie=no_cookie,
            verify_ssl=verify_ssl,
        ),
    )


__all__ = [
    "create_aiohttp_backed_httpx_client",
    "mock_router",
]
