import json

import aiohttp
import httpx
import pytest
import respx
from aioresponses import aioresponses

from httpx_aiohttp_transport import (
    AiohttpResponseStream,
    AiohttpTransport,
    create_aiohttp_backed_httpx_client,
    mock_router,
)


class TestAiohttpResponseStream:
    """Test the AiohttpResponseStream class."""

    @pytest.mark.asyncio
    async def test_aiohttp_response_stream_iter(self, monkeypatch):
        """Test that AiohttpResponseStream correctly yields chunks from the response."""
        # Create mock chunks
        chunks = [b"chunk1", b"chunk2"]

        # Create a mock aiohttp response with proper mocking of required methods
        class MockStreamReader:
            async def iter_chunked(self, chunk_size):
                for chunk in chunks:
                    yield chunk

        class MockResponse:
            content = MockStreamReader()

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        mock_response = MockResponse()

        # Test the AiohttpResponseStream
        stream = AiohttpResponseStream(mock_response)  # type: ignore
        result_chunks = []
        async for chunk in stream:
            result_chunks.append(chunk)

        assert result_chunks == chunks

        # Test aclose method
        await stream.aclose()


class TestAiohttpTransport:
    """Test the AiohttpTransport class."""

    @pytest.mark.asyncio
    async def test_transport_lifecycle(self, monkeypatch):
        """Test the lifecycle methods of AiohttpTransport."""

        # Create a mock ClientSession class
        class MockClientSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def close(self):
                pass

            async def request(self, *args, **kwargs):
                class MockResponse:
                    status = 200
                    headers = {"content-type": "text/plain"}

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, exc_type, exc_val, exc_tb):
                        pass

                    @property
                    def content(self):
                        class MockContent:
                            async def iter_chunked(self, chunk_size):
                                yield b"test"

                        return MockContent()

                return MockResponse()

        # Replace aiohttp.ClientSession with our mock
        monkeypatch.setattr(aiohttp, "ClientSession", MockClientSession)

        # Test lifecycle
        transport = AiohttpTransport()
        assert not transport._closed

        # Test __aenter__
        transport_entered = await transport.__aenter__()
        assert transport_entered is transport

        # Test __aexit__
        await transport.__aexit__(None, None, None)
        assert transport._closed

        # Test aclose
        transport = AiohttpTransport()
        await transport.aclose()
        assert transport._closed

    @pytest.mark.asyncio
    async def test_handle_async_request_closed_transport(self):
        """Test that using a closed transport raises an error."""
        transport = AiohttpTransport()
        transport._closed = True

        request = httpx.Request("GET", "https://example.com")

        with pytest.raises(RuntimeError, match="Transport is closed"):
            await transport.handle_async_request(request)

    @pytest.mark.asyncio
    async def test_handle_async_request_with_mocked_response(self):
        """Test that mocked responses are returned correctly."""
        transport = AiohttpTransport()
        request = httpx.Request("GET", "https://example.com")

        # Create a mock response
        mock_response = httpx.Response(200, content=b"mocked content")

        # Set up the mock router
        def mock_handler(req):
            return mock_response

        token = mock_router.set(mock_handler)
        try:
            response = await transport.handle_async_request(request)
            assert response is mock_response
        finally:
            mock_router.reset(token)

        await transport.aclose()

    @pytest.mark.asyncio
    async def test_handle_async_request_with_aioresponses(self):
        """Test handling a request with aioresponses."""
        with aioresponses() as m:
            m.get("https://example.com", status=200, body=b"test response")

            transport = AiohttpTransport()
            request = httpx.Request("GET", "https://example.com")

            response = await transport.handle_async_request(request)

            assert response.status_code == 200
            content = b""
            async for chunk in response.aiter_bytes():
                content += chunk
            assert content == b"test response"

            await transport.aclose()


class TestMockRouter:
    """Test the mock_router functionality."""

    @pytest.mark.asyncio
    async def test_respx_integration(self):
        """Test integration with respx."""
        with respx.mock as respx_mock:
            respx_mock.get("https://example.com").respond(
                status_code=200, content=b"mocked with respx"
            )

            token = mock_router.set(respx_mock.handler)
            try:
                transport = AiohttpTransport()
                request = httpx.Request("GET", "https://example.com")

                response = await transport.handle_async_request(request)

                assert response.status_code == 200
                content = b""
                async for chunk in response.aiter_bytes():
                    content += chunk
                assert content == b"mocked with respx"
            finally:
                mock_router.reset(token)

            await transport.aclose()

    def test_try_to_get_mocked_response_no_mock(self):
        """Test that None is returned when no mock is set."""
        from httpx_aiohttp_transport import try_to_get_mocked_response

        request = httpx.Request("GET", "https://example.com")
        result = try_to_get_mocked_response(request)

        assert result is None


class TestCreateAiohttpBackedHttpxClient:
    """Test the create_aiohttp_backed_httpx_client function."""

    @pytest.mark.asyncio
    async def test_client_creation_default_params(self):
        """Test creating a client with default parameters."""
        client = create_aiohttp_backed_httpx_client()

        assert isinstance(client, httpx.AsyncClient)
        assert isinstance(client._transport, AiohttpTransport)

        await client.aclose()

    @pytest.mark.asyncio
    async def test_client_creation_custom_params(self):
        """Test creating a client with custom parameters."""
        headers = {"User-Agent": "Test"}
        client = create_aiohttp_backed_httpx_client(
            headers=headers,
            total_timeout=30.0,
            base_url="https://api.example.com",
            proxy="http://proxy.example.com",
            keepalive_timeout=20.0,
            max_connections=200,
            max_connections_per_host=50,
            verify_ssl=True,
            login="user",
            password="pass",
            encoding="utf-8",
            force_close=True,
        )

        assert isinstance(client, httpx.AsyncClient)
        assert client._base_url == httpx.URL("https://api.example.com")

        # Check that the transport was created with the correct session
        transport = client._transport
        assert isinstance(transport, AiohttpTransport)

        await client.aclose()

    @pytest.mark.asyncio
    async def test_client_with_aioresponses(self):
        """Test the client with aioresponses."""
        with aioresponses() as m:
            m.get(
                "https://example.com",
                status=200,
                body=json.dumps({"key": "value"}),
                content_type="application/json",
            )

            client = create_aiohttp_backed_httpx_client()
            response = await client.get("https://example.com")

            assert response.status_code == 200
            assert response.json() == {"key": "value"}

            await client.aclose()
