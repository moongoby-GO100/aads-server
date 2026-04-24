import asyncio
import unittest

from scripts.claude_relay_server import _is_client_disconnect_error, _stream_prepare, _stream_write


class ClosingResponse:
    async def prepare(self, request):
        raise RuntimeError("Cannot write to closing transport")

    async def write(self, payload):
        raise RuntimeError("Cannot write to closing transport")


class RelayDisconnectTests(unittest.TestCase):
    def test_is_client_disconnect_error_handles_closing_transport_runtime_error(self):
        self.assertTrue(_is_client_disconnect_error(RuntimeError("Cannot write to closing transport")))

    def test_is_client_disconnect_error_ignores_other_runtime_errors(self):
        self.assertFalse(_is_client_disconnect_error(RuntimeError("unexpected failure")))

    def test_stream_write_normalizes_closing_transport_runtime_error(self):
        async def _run():
            with self.assertRaises(ConnectionResetError):
                await _stream_write(ClosingResponse(), b"test")

        asyncio.get_event_loop().run_until_complete(_run())

    def test_stream_prepare_normalizes_closing_transport_runtime_error(self):
        async def _run():
            with self.assertRaises(ConnectionResetError):
                await _stream_prepare(ClosingResponse(), object())

        asyncio.get_event_loop().run_until_complete(_run())


if __name__ == "__main__":
    unittest.main()
