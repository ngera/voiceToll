"""Collector command line helpers."""

from __future__ import annotations

import logging
import sys
import unittest

from voicetoll_collector.cli import _drop_client_disconnects


def _record(message: str, exc: BaseException) -> logging.LogRecord:
    try:
        raise exc
    except BaseException:
        info = sys.exc_info()
    return logging.LogRecord("asyncio", logging.ERROR, __file__, 0, message, None, info)


class ClientDisconnectFilterTests(unittest.TestCase):
    def test_windows_disconnect_noise_is_hidden(self):
        record = _record(
            "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)",
            ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host"),
        )
        self.assertFalse(_drop_client_disconnects(record))

    def test_other_asyncio_errors_still_show(self):
        self.assertTrue(
            _drop_client_disconnects(_record("Task exception was never retrieved", ConnectionResetError()))
        )
        self.assertTrue(
            _drop_client_disconnects(_record("Exception in callback _call_connection_lost", ValueError()))
        )


if __name__ == "__main__":
    unittest.main()
