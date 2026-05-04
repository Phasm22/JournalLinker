import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "telegram_smoke_test.py"
spec = importlib.util.spec_from_file_location("telegram_smoke_test", SCRIPT)
ts = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(ts)


class TestTelegramSmoke(unittest.TestCase):
    def test_missing_token_returns_10(self):
        with mock.patch.object(ts, "load_env_files"), mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ts.run_smoke(send_text=None, quiet=True), 10)

    @mock.patch.object(ts, "tg_request")
    @mock.patch.object(ts, "load_env_files")
    def test_getme_only_returns_0(self, _lf, tg_mock):
        tg_mock.return_value = {"ok": True, "result": {"id": 1, "username": "bot"}}
        with mock.patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "123"},
            clear=False,
        ):
            self.assertEqual(ts.run_smoke(send_text=None, quiet=True), 0)
        tg_mock.assert_called_once()
        self.assertEqual(tg_mock.call_args[0][1], "getMe")

    @mock.patch.object(ts, "tg_request")
    @mock.patch.object(ts, "load_env_files")
    def test_send_calls_sendmessage(self, _lf, tg_mock):
        def side_effect(token: str, method: str, payload: dict, *, timeout: int = 30):
            if method == "getMe":
                return {"ok": True, "result": {"id": 1, "username": "bot"}}
            if method == "sendMessage":
                return {"ok": True, "result": {"message_id": 99}}
            raise AssertionError(method)

        tg_mock.side_effect = side_effect
        with mock.patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "123"},
            clear=False,
        ):
            self.assertEqual(ts.run_smoke(send_text="hi", quiet=True), 0)
        self.assertEqual(tg_mock.call_count, 2)
        self.assertEqual(tg_mock.call_args_list[1][0][1], "sendMessage")
