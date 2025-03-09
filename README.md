[![codecov](https://codecov.io/gh/lizeyan/httpx-AIOHttpTransport/graph/badge.svg?token=N0RSAD0V7C)](https://codecov.io/gh/lizeyan/httpx-AIOHttpTransport)
[![PyPI version](https://badge.fury.io/py/httpx-aiohttptransport.svg)](https://badge.fury.io/py/httpx-aiohttptransport)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/httpx-aiohttptransport.svg)](https://pypi.org/project/httpx-aiohttptransport/)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/httpx-aiohttptransport.svg)](https://pypi.org/project/httpx-aiohttptransport/)
[![PyPI - License](https://img.shields.io/pypi/l/httpx-aiohttptransport.svg)](https://pypi.org/project/httpx-aiohttptransport/)
[![Code style: black](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)


# Use `aiohttp` with `httpx` Interface

`httpx` has performance issue, especially when working with high concurrency, while `aiohttp` does not.

However, your production code and tests may already heavily rely on `httpx`, making it difficult to migrate to
`aiohttp`.

This repo provides a workaround: take advantage of `httpx`'s custom transport capability to use `aiohttp` for the actual
requests

```shell
pip install httpx-aiohttptransport
```

This package supports:

- transport limits (max connection)
- auth
- proxy
- `respx`. Run `mock_router.set(router.handler)` when you set up the respx mock router (see example). 

Known limitations:

- http2. `aiohttp` does not support http2.
