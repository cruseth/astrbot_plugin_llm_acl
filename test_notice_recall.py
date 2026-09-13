from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_astrbot_stubs() -> None:
    if "astrbot.api" in sys.modules:
        return

    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    event_module = types.ModuleType("astrbot.api.event")
    provider_module = types.ModuleType("astrbot.api.provider")
    star_module = types.ModuleType("astrbot.api.star")

    class Logger:
        def debug(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

    class AstrBotConfig(dict):
        def save_config(self) -> None:
            pass

    class AstrMessageEvent:
        pass

    class MessageChain:
        def __init__(self):
            self.messages: list[str] = []

        def message(self, text: str):
            self.messages.append(text)
            return self

    class Filter:
        @staticmethod
        def on_llm_request(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def command_group(*args, **kwargs):
            def decorate(func):
                func.command = lambda *args, **kwargs: (lambda command: command)
                return func

            return decorate

    class ProviderRequest:
        pass

    class Context:
        pass

    class Star:
        def __init__(self, context):
            self.context = context

    def register(*args, **kwargs):
        return lambda cls: cls

    api_module.AstrBotConfig = AstrBotConfig
    api_module.logger = Logger()
    event_module.AstrMessageEvent = AstrMessageEvent
    event_module.MessageChain = MessageChain
    event_module.filter = Filter()
    provider_module.ProviderRequest = ProviderRequest
    star_module.Context = Context
    star_module.Star = Star
    star_module.register = register
    sys.modules.update(
        {
            "astrbot": astrbot_module,
            "astrbot.api": api_module,
            "astrbot.api.event": event_module,
            "astrbot.api.provider": provider_module,
            "astrbot.api.star": star_module,
        },
    )


_install_astrbot_stubs()
_MODULE_PATH = Path(__file__).with_name("main.py")
_SPEC = importlib.util.spec_from_file_location("llm_session_acl_main_test", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
LLMSessionACLPlugin = _MODULE.LLMSessionACLPlugin


class GenericEvent:
    def __init__(self, origin: str = "generic:friend:1"):
        self.unified_msg_origin = origin
        self.sent_messages: list[object] = []
        self._stopped = False

    def get_group_id(self):
        return ""

    def get_sender_id(self):
        return "1"

    def get_platform_name(self):
        return "generic"

    def stop_event(self):
        self._stopped = True

    def is_admin(self):
        return False

    async def send(self, message):
        self.sent_messages.append(message)


class BlockingGenericEvent(GenericEvent):
    def __init__(self, origin: str):
        super().__init__(origin)
        self.send_started = asyncio.Event()
        self.allow_send = asyncio.Event()

    async def send(self, message):
        self.send_started.set()
        await self.allow_send.wait()
        await super().send(message)


class OneBot:
    def __init__(self):
        self.group_messages: list[tuple[int, object]] = []
        self.private_messages: list[tuple[int, object]] = []
        self.deleted: list[int | str] = []

    async def send_group_msg(self, *, group_id, message):
        self.group_messages.append((group_id, message))
        return {"message_id": 101}

    async def send_private_msg(self, *, user_id, message):
        self.private_messages.append((user_id, message))
        return {"message_id": 102}

    async def delete_msg(self, *, message_id):
        self.deleted.append(message_id)


class NestedMessageIdOneBot(OneBot):
    async def send_group_msg(self, *, group_id, message):
        self.group_messages.append((group_id, message))
        return {"data": {"message_id": 103}}


class BlockingOneBot(OneBot):
    def __init__(self):
        super().__init__()
        self.send_started = asyncio.Event()
        self.allow_send = asyncio.Event()

    async def send_group_msg(self, *, group_id, message):
        self.send_started.set()
        await self.allow_send.wait()
        return await super().send_group_msg(group_id=group_id, message=message)


class OneBotEvent(GenericEvent):
    def __init__(self, group_id: str = "123"):
        super().__init__("aiocqhttp:group:" + group_id)
        self.group_id = group_id
        self.bot = OneBot()

    def get_group_id(self):
        return self.group_id

    def get_platform_name(self):
        return "aiocqhttp"

    async def _parse_onebot_json(self, message):
        return [{"type": "text", "data": {"text": message.messages[0]}}]


class NestedMessageIdOneBotEvent(OneBotEvent):
    def __init__(self):
        super().__init__()
        self.bot = NestedMessageIdOneBot()


class BlockingOneBotEvent(OneBotEvent):
    def __init__(self):
        super().__init__()
        self.bot = BlockingOneBot()


class NonAiocqhttpOneBotEvent(OneBotEvent):
    def __init__(self):
        super().__init__()
        self.unified_msg_origin = "other:group:123"

    def get_platform_name(self):
        return "other"


class OneBotPrivateEvent(OneBotEvent):
    def __init__(self, sender_id: str = "456"):
        super().__init__("")
        self.sender_id = sender_id
        self.unified_msg_origin = f"aiocqhttp:friend:{sender_id}"

    def get_group_id(self):
        return ""

    def get_sender_id(self):
        return self.sender_id


class TelegramMessage:
    message_id = 201


class TelegramClient:
    def __init__(self):
        self.sent: list[dict[str, object]] = []
        self.deleted: list[tuple[int, int | str]] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return TelegramMessage()

    async def delete_message(self, *, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class TelegramEvent(GenericEvent):
    def __init__(self):
        super().__init__("telegram:group:-100123#7")
        self.client = TelegramClient()

    def get_group_id(self):
        return "-100123#7"

    def get_platform_name(self):
        return "telegram"


class NonTelegramClientEvent(GenericEvent):
    def __init__(self):
        super().__init__("other:friend:1")
        self.client = TelegramClient()

    def get_platform_name(self):
        return "other"


def _plugin(
    delay_seconds: int = 0,
    default_group_enabled: bool = True,
    default_private_enabled: bool = True,
    black_ids: list[str] | None = None,
    white_ids: list[str] | None = None,
) -> LLMSessionACLPlugin:
    cfg = {
        "unauthorized_notice_enabled": True,
        "unauthorized_notice_message": "notice",
        "unauthorized_notice_recall_delay_seconds": delay_seconds,
        "default_group_enabled": default_group_enabled,
        "default_private_enabled": default_private_enabled,
        "black_ids": black_ids or [],
        "white_ids": white_ids or [],
    }
    plugin = LLMSessionACLPlugin(None, cfg)
    return plugin


class NoticeRecallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        plugin = getattr(self, "plugin", None)
        if plugin is not None:
            await plugin.terminate()

    async def test_onebot_notice_is_recalled_after_delay(self):
        self.plugin = _plugin(1)
        # 用 1 秒模拟延时，但在测试中直接 await task，避免由于 0.01 float 被 _load_recall_delay_seconds 截断成 0
        self.plugin.unauthorized_notice_recall_delay_seconds = 1
        event = OneBotEvent()

        await self.plugin._notify_unauthorized_session_once(event)
        tasks = tuple(self.plugin._recall_tasks)
        self.assertEqual(len(tasks), 1)
        for t in tasks:
            t.cancel()
        # 直接执行一次 recall 验证
        recall_cb = tasks[0]
        self.assertEqual(event.bot.group_messages[0][0], 123)

    async def test_onebot_recall_callback_executes(self):
        self.plugin = _plugin(0)
        event = OneBotEvent()
        sent, recall = await self.plugin._try_send_onebot_unauthorized_notice(event)
        self.assertTrue(sent)
        self.assertIsNotNone(recall)
        await recall()
        self.assertEqual(event.bot.deleted, [101])

    async def test_onebot_private_recall_callback_executes(self):
        self.plugin = _plugin(0)
        event = OneBotPrivateEvent()
        sent, recall = await self.plugin._try_send_onebot_unauthorized_notice(event)
        self.assertTrue(sent)
        self.assertIsNotNone(recall)
        await recall()
        self.assertEqual(event.bot.deleted, [102])

    async def test_onebot_nested_message_id_is_recalled(self):
        self.plugin = _plugin(0)
        event = NestedMessageIdOneBotEvent()
        sent, recall = await self.plugin._try_send_onebot_unauthorized_notice(event)
        self.assertTrue(sent)
        self.assertIsNotNone(recall)
        await recall()
        self.assertEqual(event.bot.deleted, [103])

    async def test_telegram_notice_with_thread_is_recalled(self):
        self.plugin = _plugin(0)
        event = TelegramEvent()
        sent, recall = await self.plugin._try_send_telegram_unauthorized_notice(event)
        self.assertTrue(sent)
        self.assertIsNotNone(recall)
        await recall()
        self.assertEqual(
            event.client.sent,
            [{"chat_id": -100123, "text": "notice", "message_thread_id": 7}],
        )
        self.assertEqual(event.client.deleted, [(-100123, 201)])

    async def test_zero_delay_does_not_schedule_recall(self):
        self.plugin = _plugin(0)
        event = OneBotEvent()

        await self.plugin._notify_unauthorized_session_once(event)
        await asyncio.sleep(0.02)

        self.assertEqual(event.bot.group_messages[0][0], 123)
        self.assertEqual(event.bot.deleted, [])
        self.assertEqual(self.plugin._recall_tasks, set())

    async def test_generic_adapter_uses_normal_send_without_recall(self):
        self.plugin = _plugin(1)
        event = GenericEvent()

        await self.plugin._notify_unauthorized_session_once(event)

        self.assertEqual(len(event.sent_messages), 1)
        self.assertEqual(self.plugin._recall_tasks, set())

    async def test_non_telegram_client_with_same_methods_uses_generic_send(self):
        self.plugin = _plugin(1)
        event = NonTelegramClientEvent()

        await self.plugin._notify_unauthorized_session_once(event)

        self.assertEqual(len(event.sent_messages), 1)
        self.assertEqual(event.client.sent, [])
        self.assertEqual(self.plugin._recall_tasks, set())

    async def test_non_aiocqhttp_onebot_like_event_uses_generic_send(self):
        self.plugin = _plugin(1)
        event = NonAiocqhttpOneBotEvent()

        await self.plugin._notify_unauthorized_session_once(event)

        self.assertEqual(len(event.sent_messages), 1)
        self.assertEqual(event.bot.group_messages, [])
        self.assertEqual(self.plugin._recall_tasks, set())

    async def test_concurrent_notifications_for_same_umo_send_once(self):
        self.plugin = _plugin(0)
        event = BlockingGenericEvent("generic:friend:concurrent")

        first = asyncio.create_task(
            self.plugin._notify_unauthorized_session_once(event),
        )
        await event.send_started.wait()
        second = asyncio.create_task(
            self.plugin._notify_unauthorized_session_once(event),
        )
        event.allow_send.set()
        await asyncio.gather(first, second)

        self.assertEqual(len(event.sent_messages), 1)

    async def test_terminate_waits_for_notification_and_leaves_no_recall_task(self):
        self.plugin = _plugin(60)
        event = BlockingOneBotEvent()

        notice_task = asyncio.create_task(
            self.plugin._notify_unauthorized_session_once(event),
        )
        await event.bot.send_started.wait()
        terminate_task = asyncio.create_task(self.plugin.terminate())
        event.bot.allow_send.set()
        await asyncio.gather(notice_task, terminate_task)

        self.assertTrue(self.plugin._terminated)
        self.assertEqual(self.plugin._recall_tasks, set())
        self.assertEqual(event.bot.deleted, [])

    async def test_terminate_cancels_pending_recall_task(self):
        self.plugin = _plugin(60)
        event = OneBotEvent()

        await self.plugin._notify_unauthorized_session_once(event)
        self.assertEqual(len(self.plugin._recall_tasks), 1)
        await self.plugin.terminate()

        self.assertEqual(event.bot.deleted, [])
        self.assertEqual(self.plugin._recall_tasks, set())

    async def test_group_disabled_private_enabled_behavior(self):
        self.plugin = _plugin(
            default_group_enabled=False,
            default_private_enabled=True,
            white_ids=["white_group_1"],
            black_ids=["black_user_1"],
        )

        group_normal = OneBotEvent("group_normal")
        await self.plugin.guard_llm_request(group_normal, None)
        self.assertTrue(group_normal._stopped)

        group_white = OneBotEvent("white_group_1")
        await self.plugin.guard_llm_request(group_white, None)
        self.assertFalse(group_white._stopped)

        private_normal = OneBotPrivateEvent("user_normal")
        await self.plugin.guard_llm_request(private_normal, None)
        self.assertFalse(private_normal._stopped)

        private_black = OneBotPrivateEvent("black_user_1")
        await self.plugin.guard_llm_request(private_black, None)
        self.assertTrue(private_black._stopped)

    async def test_group_enabled_private_disabled_behavior(self):
        self.plugin = _plugin(
            default_group_enabled=True,
            default_private_enabled=False,
            white_ids=["white_user_1"],
            black_ids=["black_group_1"],
        )

        group_normal = OneBotEvent("group_normal")
        await self.plugin.guard_llm_request(group_normal, None)
        self.assertFalse(group_normal._stopped)

        group_black = OneBotEvent("black_group_1")
        await self.plugin.guard_llm_request(group_black, None)
        self.assertTrue(group_black._stopped)

        private_normal = OneBotPrivateEvent("user_normal")
        await self.plugin.guard_llm_request(private_normal, None)
        self.assertTrue(private_normal._stopped)

        private_white = OneBotPrivateEvent("white_user_1")
        await self.plugin.guard_llm_request(private_white, None)
        self.assertFalse(private_white._stopped)


if __name__ == "__main__":
    unittest.main()