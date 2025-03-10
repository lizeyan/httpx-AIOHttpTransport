import typing
from contextvars import ContextVar

import httpx

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
