import asyncio
import typing
from contextvars import ContextVar
from types import TracebackType

import aiohttp
import httpx
from httpx import AsyncBaseTransport

AIOHTTP_TO_HTTPX_EXCEPTIONS: dict[type[Exception], type[Exception]] = {
    # Order matters here, most specific exception first
    aiohttp.ClientConnectorDNSError: httpx.ConnectError,
    aiohttp.ClientProxyConnectionError: httpx.ProxyError,
    aiohttp.ClientConnectorCertificateError: httpx.ProtocolError,
    aiohttp.ClientSSLError: httpx.ProtocolError,
    aiohttp.ServerFingerprintMismatch: httpx.ProtocolError,
    aiohttp.ClientConnectionResetError: httpx.ConnectError,
    aiohttp.ClientConnectorError: httpx.ConnectError,
    aiohttp.ClientOSError: httpx.ConnectError,
    aiohttp.ServerDisconnectedError: httpx.ReadError,
    aiohttp.ClientConnectionError: httpx.NetworkError,
    aiohttp.ClientPayloadError: httpx.ReadError,
    aiohttp.ContentTypeError: httpx.ReadError,
    aiohttp.TooManyRedirects: httpx.TooManyRedirects,
    aiohttp.InvalidURL: httpx.InvalidURL,
    aiohttp.ClientError: httpx.RequestError,
}


async def transform_response_to_httpx(
    aiohttp_response: aiohttp.ClientResponse, httpx_request: httpx.Request
) -> httpx.Response:
    content = await aiohttp_response.read()
    headers = [
        (k.lower(), v)
        for k, v in aiohttp_response.headers.items()
        if k.lower() != "content-encoding"
    ]

    return httpx.Response(
        status_code=aiohttp_response.status,
        headers=headers,
        content=content,
        request=httpx_request,
    )


async def map_aiohttp_exception(exc: Exception, httpx_request: httpx.Request) -> Exception:
    for aiohttp_exc, httpx_exc in AIOHTTP_TO_HTTPX_EXCEPTIONS.items():
        if isinstance(exc, aiohttp_exc):
            return httpx_exc(str(exc))

    if isinstance(exc, asyncio.TimeoutError):
        return httpx.TimeoutException(str(exc))

    return httpx.HTTPError(f"Unknown error: {str(exc)}")


class AIOHTTPTransport(AsyncBaseTransport):
    def __init__(self, session: aiohttp.ClientSession | None = None):
        self._session = session or aiohttp.ClientSession()
        self._closed = False

    async def __aenter__(self) -> typing.Self:
        await self._session.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ):
        await self._session.__aexit__(exc_type, exc_value, traceback)
        self._closed = True

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._closed:
            raise RuntimeError("Transport is closed")

        try:
            headers = dict(request.headers)

            method = request.method
            url = str(request.url)
            content = request.content

            async with self._session.request(
                method=method,
                url=url,
                headers=headers,
                data=content,
                allow_redirects=True,
            ) as aiohttp_response:
                return await transform_response_to_httpx(aiohttp_response, request)
        except Exception as e:
            raise await map_aiohttp_exception(e, request) from e


mock_router: ContextVar[typing.Callable[[httpx.Request], httpx.Response]] = ContextVar(
    "mock_router"
)


def try_to_get_mocked_response(request: httpx.Request) -> httpx.Response | None:
    try:
        _mock_handler = mock_router.get()
    except LookupError:
        return None
    return _mock_handler(request)


def create_aiohttp_backed_httpx_client(
    *,
    headers: dict[str, str] | None = None,
    total_timeout: float | None = None,
    base_url: str = "",
    proxy: str | None = None,
    keepalive_timeout: float = 15,
    max_connections: int = 100,
    max_connections_per_host: int = 0,
    verify_ssl: bool = False,
    login: str | None = None,
    password: str | None = None,
    encoding: str = "latin1",
    force_close: bool = False,
) -> httpx.AsyncClient:
    timeout = aiohttp.ClientTimeout(total=total_timeout)
    connector = aiohttp.TCPConnector(
        keepalive_timeout=keepalive_timeout if not force_close else None,
        limit=max_connections,
        limit_per_host=max_connections_per_host,
        ssl=verify_ssl,
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
        verify=False,
        transport=AIOHTTPTransport(
            session=aiohttp.ClientSession(
                proxy=proxy,
                auth=auth,
                timeout=timeout,
                connector=connector,
                headers=headers,
            )
        ),
    )


__all__ = [
    "create_aiohttp_backed_httpx_client",
    "mock_router",
]
