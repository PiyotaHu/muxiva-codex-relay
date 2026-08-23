#!/usr/bin/env python3
"""Expose a reverse SSH tunnel to LAN clients without opening Windows Firewall."""

import asyncio
import contextlib

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8765
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 18765


async def copy_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            UPSTREAM_HOST, UPSTREAM_PORT
        )
    except OSError:
        client_writer.close()
        await client_writer.wait_closed()
        return

    await asyncio.gather(
        copy_stream(client_reader, upstream_writer),
        copy_stream(upstream_reader, client_writer),
        return_exceptions=True,
    )


async def main() -> None:
    server = await asyncio.start_server(handle, LISTEN_HOST, LISTEN_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
