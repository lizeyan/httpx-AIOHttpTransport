import asyncio
from unittest.mock import AsyncMock, Mock

import aiohttp
import httpx
import pytest
from aiohttp.client_reqrep import ConnectionKey

from httpx_aiohttp_transport import AIOHTTP_TO_HTTPX_EXCEPTIONS, AIOHTTPTransport

connection_key = ConnectionKey(
    host="http://example.com",
    port=8000,
    proxy=None,
    proxy_auth=None,
    proxy_headers_hash=None,
    is_ssl=False,
    ssl=False,
)
os_error = OSError()


def get_exception(exc_class):  # noqa: C901
    match exc_class:
        case aiohttp.ClientConnectorDNSError:
            return aiohttp.ClientConnectorDNSError(connection_key, os_error)
        case aiohttp.ClientProxyConnectionError:
            return aiohttp.ClientProxyConnectionError(connection_key, os_error)
        case aiohttp.ClientConnectorCertificateError:
            return aiohttp.ClientConnectorCertificateError(connection_key, os_error)
        case aiohttp.ClientSSLError:
            return aiohttp.ClientSSLError(connection_key, os_error)
        case aiohttp.ServerFingerprintMismatch:
            return aiohttp.ServerFingerprintMismatch(
                expected=b"", got=b"", host="http://example.com", port=8000
            )
        case aiohttp.ClientConnectionResetError:
            return aiohttp.ClientConnectionResetError(connection_key, os_error)
        case aiohttp.ClientConnectorError:
            return aiohttp.ClientConnectorError(connection_key, os_error)
        case aiohttp.ClientOSError:
            return aiohttp.ClientOSError()
        case aiohttp.ClientConnectionError:
            return aiohttp.ClientConnectionError()
        case aiohttp.ServerDisconnectedError:
            return aiohttp.ServerDisconnectedError()
        case aiohttp.ClientPayloadError:
            return aiohttp.ClientPayloadError()
        case aiohttp.ContentTypeError:
            return aiohttp.ContentTypeError(request_info=Mock(), history=None)
        case aiohttp.ClientResponseError:
            return aiohttp.ClientResponseError(request_info=Mock(), history=None)
        case aiohttp.TooManyRedirects:
            return aiohttp.TooManyRedirects(request_info=Mock(), history=None)
        case aiohttp.InvalidURL:
            return aiohttp.InvalidURL(url="http://example.com")
        case aiohttp.ClientError:
            return aiohttp.ClientError()


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient(transport=AIOHTTPTransport()) as http_client:
        yield http_client


async def test_plain_text_successful_request(http_client, aioresponses):
    aioresponses.get("http://example.com", status=200, body="test")

    response = await http_client.get("http://example.com")

    assert response.status_code == 200
    assert response.text == "test"


async def test_json_successful_request(http_client, aioresponses):
    test_data = {"test": "data"}
    aioresponses.get("http://example.com", status=200, payload=test_data)

    response = await http_client.get("http://example.com")

    assert response.status_code == 200
    assert response.json() == test_data


@pytest.mark.parametrize("aiohttp_exc, httpx_exc", AIOHTTP_TO_HTTPX_EXCEPTIONS.items())
async def test_exception_mapping(aiohttp_exc, httpx_exc):
    exc_instance = get_exception(aiohttp_exc)
    mock_response = AsyncMock()
    mock_response.__aenter__.side_effect = exc_instance

    session = Mock(spec=aiohttp.ClientSession)
    session.request.return_value = mock_response

    request = httpx.Request("GET", "http://example.com")

    transport = AIOHTTPTransport(session)

    with pytest.raises(httpx_exc) as exc_info:
        await transport.handle_async_request(request)

    assert str(exc_instance) in str(exc_info.value)


@pytest.mark.parametrize("aiohttp_exc, httpx_exc", [(asyncio.TimeoutError, httpx.TimeoutException)])
async def test_timeout(aiohttp_exc, httpx_exc):
    mock_response = AsyncMock()
    mock_response.__aenter__.side_effect = aiohttp_exc

    session = Mock(spec=aiohttp.ClientSession)
    session.request.return_value = mock_response

    request = httpx.Request("GET", "http://example.com")

    transport = AIOHTTPTransport(session)

    with pytest.raises(httpx_exc):
        await transport.handle_async_request(request)


@pytest.mark.parametrize("aiohttp_exc, httpx_exc", [(Exception, httpx.HTTPError)])
async def test_unknown_error(aiohttp_exc, httpx_exc):
    mock_response = AsyncMock()
    mock_response.__aenter__.side_effect = aiohttp_exc

    session = Mock(spec=aiohttp.ClientSession)
    session.request.return_value = mock_response

    request = httpx.Request("GET", "http://example.com")

    transport = AIOHTTPTransport(session)

    with pytest.raises(httpx_exc, match="Unknown error"):
        await transport.handle_async_request(request)


bad_codes = [x for x in httpx.codes if x.value >= 400]
good_codes = [x for x in httpx.codes if x.value < 400]


@pytest.mark.parametrize("status", bad_codes)
async def test_bad_http_errors(http_client, aioresponses, status):
    aioresponses.get("http://example.com", status=status.value, body="text")

    response = await http_client.get("http://example.com")
    assert isinstance(response, httpx.Response)
    assert isinstance(response.request, httpx.Request)
    assert response.status_code == status.value
    assert response.text == "text"

    with pytest.raises(httpx.HTTPError, match=status.phrase):
        response.raise_for_status()


@pytest.mark.parametrize("status", good_codes)
async def test_good_http_errors(http_client, aioresponses, status):
    aioresponses.get("http://example.com", status=status.value, payload={"test": "data"})

    response = await http_client.get("http://example.com")
    assert response.status_code == status.value
    assert response.json() == {"test": "data"}
    assert response.request.url == "http://example.com"


async def test_close_already_closed_transport():
    async with AIOHTTPTransport() as transport:
        ...

    with pytest.raises(RuntimeError, match="Transport is closed"):
        await transport.handle_async_request(Mock())
