import asyncio
import unittest

import httpx2

from runtime.provider_gateway import _DoneCheckedSSEStream


class ProviderSSEProtocolTests(unittest.TestCase):
    def test_missing_done_marker_is_protocol_error(self):
        stream = _DoneCheckedSSEStream(httpx2.ByteStream(
            b'data: {"choices":[]}\n\n'
        ))

        async def consume():
            return [chunk async for chunk in stream]

        with self.assertRaises(httpx2.RemoteProtocolError) as caught:
            asyncio.run(consume())
        self.assertIn("without the data: [DONE]", str(caught.exception))

    def test_done_marker_may_be_split_across_network_chunks(self):
        class Chunks(httpx2.AsyncByteStream):
            async def __aiter__(self):
                yield b"data: [DO"
                yield b"NE]\n\n"

            async def aclose(self):
                return None

        stream = _DoneCheckedSSEStream(Chunks())

        async def consume():
            return [chunk async for chunk in stream]

        self.assertEqual(asyncio.run(consume()), [b"data: [DO", b"NE]\n\n"])


if __name__ == "__main__":
    unittest.main()
