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
