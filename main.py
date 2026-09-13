from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


RecallCallback = Callable[[], Awaitable[None]]


@register("astrbot_plugin_llm_session_acl", "cruseth", "群组/用户级 LLM 黑白名单与会话控制", "v1.2.0")
class LLMSessionACLPlugin(Star):
    """按群组 ID 或用户 ID 控制 LLM 访问。

    优先级：
    1. 群聊的群组 ID 或私聊的用户 ID 命中 black_ids 时强制关闭 LLM
    2. 群聊的群组 ID 或私聊的用户 ID 命中 white_ids 时强制开启 LLM
    3. 其余消息按场景区分：群聊走 default_group_enabled，私聊走 default_private_enabled
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.admin_users = self._load_id_set("admin_users")
        self.black_ids = self._load_id_set("black_ids", "black_sessions")
        self.white_ids = self._load_id_set("white_ids", "white_sessions")

        legacy_default = self.config.get("default_enabled", None)
        default_fallback = bool(legacy_default) if legacy_default is not None else True
        self.default_group_enabled = bool(
            self.config.get("default_group_enabled", default_fallback),
        )
        self.default_private_enabled = bool(
            self.config.get("default_private_enabled", default_fallback),
        )

        self.unauthorized_notice_enabled = bool(
            self.config.get("unauthorized_notice_enabled", True),
        )
        self.unauthorized_notice_message = str(
            self.config.get(
                "unauthorized_notice_message",
                "当前会话未获 LLM 使用授权，请联系管理员。",
            )
            or "",
        ).strip()
        self.unauthorized_notice_recall_delay_seconds = (
            self._load_recall_delay_seconds()
        )
        self._notified_unified_msg_origins: set[str] = set()
        self._notification_lock = asyncio.Lock()
        self._recall_tasks: set[asyncio.Task[None]] = set()
        self._terminated = False

    def _load_id_set(self, key: str, legacy_key: str | None = None) -> set[str]:
        values = self.config.get(key, None)
        if values is None and legacy_key:
            values = self.config.get(legacy_key, [])
        return {str(x).strip() for x in values or [] if str(x).strip()}

    def _load_recall_delay_seconds(self) -> int:
        value = self.config.get("unauthorized_notice_recall_delay_seconds", 0)
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value if value > 0 else 0
        if isinstance(value, str):
            normalized = value.strip()
            if normalized.lstrip("+-").isdigit():
                parsed = int(normalized)
                return parsed if parsed > 0 else 0
        return 0

    def _save(self) -> None:
        self.config["admin_users"] = sorted(self.admin_users)
        self.config["black_ids"] = sorted(self.black_ids)
        self.config["white_ids"] = sorted(self.white_ids)
        self.config["default_group_enabled"] = self.default_group_enabled
        self.config["default_private_enabled"] = self.default_private_enabled
        self.config.pop("default_enabled", None)
        self.config.save_config()

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        uid = str(event.get_sender_id())
        return bool(event.is_admin()) or uid in self.admin_users

    def _target_ids(self, event: AstrMessageEvent) -> tuple[str, ...]:
        group_id = str(event.get_group_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        if group_id:
            return (group_id,)
        if sender_id:
            return (sender_id,)
        return ()

    def _current_scope_label(self, event: AstrMessageEvent) -> str:
        group_id = str(event.get_group_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        if group_id:
            return f"群组ID: {group_id}\n用户ID: {sender_id or '(未知)'}"
        return f"用户ID: {sender_id or '(未知)'}"

    def _default_target_id(self, event: AstrMessageEvent) -> str:
        group_id = str(event.get_group_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        return group_id or sender_id

    def _is_llm_enabled_for_event(self, event: AstrMessageEvent) -> bool:
        target_ids = self._target_ids(event)
        is_group = bool(str(event.get_group_id() or "").strip())
        return self._is_llm_enabled(target_ids, is_group=is_group)

    def _is_llm_enabled(self, target_ids: tuple[str, ...], is_group: bool) -> bool:
        if any(target_id in self.black_ids for target_id in target_ids):
            return False
        if any(target_id in self.white_ids for target_id in target_ids):
            return True
        return self.default_group_enabled if is_group else self.default_private_enabled

    @staticmethod
    def _extract_message_id(response: Any) -> int | str | None:
        message_id: Any = None
        if isinstance(response, dict):
            message_id = response.get("message_id")
            if message_id is None and isinstance(response.get("data"), dict):
                message_id = response["data"].get("message_id")
        else:
            message_id = getattr(response, "message_id", None)

        if isinstance(message_id, bool) or message_id is None:
            return None
        if isinstance(message_id, int):
            return message_id
        if isinstance(message_id, str) and message_id.strip():
            return message_id
        return None

    async def _try_send_onebot_unauthorized_notice(
        self,
        event: AstrMessageEvent,
    ) -> tuple[bool, RecallCallback | None] | None:
        if str(event.get_platform_name() or "") != "aiocqhttp":
            return None
        parser = getattr(event, "_parse_onebot_json", None)
        bot = getattr(event, "bot", None)
        if not callable(parser) or bot is None:
            return None
        if not all(
            callable(getattr(bot, method, None))
            for method in ("send_group_msg", "send_private_msg", "delete_msg")
        ):
            return None

        group_id = str(event.get_group_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        target_id = group_id or sender_id
        if not target_id.isdigit():
            logger.debug(
                "[llm_acl] OneBot notice fallback: invalid target id: %s",
                target_id,
            )
            return None

        try:
            message = await parser(
                MessageChain().message(self.unauthorized_notice_message),
            )
            if not message:
                raise ValueError("empty OneBot notice message")
            if group_id:
                response = await bot.send_group_msg(
                    group_id=int(group_id),
                    message=message,
                )
            else:
                response = await bot.send_private_msg(
                    user_id=int(sender_id),
                    message=message,
                )
        except Exception:
            logger.warning(
                "[llm_acl] failed to send OneBot unauthorized notice",
                exc_info=True,
            )
            return False, None

        message_id = self._extract_message_id(response)
        if message_id is None:
            logger.debug(
                "[llm_acl] OneBot notice sent without a recallable message id",
            )
            return True, None

        async def recall() -> None:
            await bot.delete_msg(message_id=message_id)

        return True, recall

    async def _try_send_telegram_unauthorized_notice(
        self,
        event: AstrMessageEvent,
    ) -> tuple[bool, RecallCallback | None] | None:
        if str(event.get_platform_name() or "") != "telegram":
            return None
        client = getattr(event, "client", None)
        if client is None:
            return None
        if not all(
            callable(getattr(client, method, None))
            for method in ("send_message", "delete_message")
        ):
            return None

        group_id = str(event.get_group_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        target = group_id or sender_id
        chat_id_text, separator, thread_id_text = target.partition("#")
        try:
            chat_id = int(chat_id_text)
            message_thread_id = int(thread_id_text) if separator else None
        except ValueError:
            logger.debug(
                "[llm_acl] Telegram notice fallback: invalid chat target: %s",
                target,
            )
            return None

        try:
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": self.unauthorized_notice_message,
            }
            if message_thread_id is not None:
                payload["message_thread_id"] = message_thread_id
            response = await client.send_message(**payload)
        except Exception:
            logger.warning(
                "[llm_acl] failed to send Telegram unauthorized notice",
                exc_info=True,
            )
            return False, None

        message_id = self._extract_message_id(response)
        if message_id is None:
            logger.debug(
                "[llm_acl] Telegram notice sent without a recallable message id",
            )
            return True, None

        async def recall() -> None:
            await client.delete_message(chat_id=chat_id, message_id=message_id)

        return True, recall

    async def _recall_notice_after_delay(
        self,
        delay_seconds: int,
        recall: RecallCallback,
    ) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            await recall()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "[llm_acl] failed to recall unauthorized notice",
                exc_info=True,
            )

    def _schedule_notice_recall(self, recall: RecallCallback) -> None:
        if (
            self._terminated
            or self.unauthorized_notice_recall_delay_seconds <= 0
        ):
            return
        try:
            task = asyncio.create_task(
                self._recall_notice_after_delay(
                    self.unauthorized_notice_recall_delay_seconds,
                    recall,
                ),
            )
        except Exception:
            logger.warning(
                "[llm_acl] failed to schedule unauthorized notice recall",
                exc_info=True,
            )
            return
        self._recall_tasks.add(task)
        task.add_done_callback(self._recall_tasks.discard)

    async def _notify_unauthorized_session_once(self, event: AstrMessageEvent) -> None:
        if (
            not self.unauthorized_notice_enabled
            or not self.unauthorized_notice_message
        ):
            return

        unified_msg_origin = str(event.unified_msg_origin or "").strip()
        if not unified_msg_origin:
            logger.warning(
                "[llm_acl] skipped unauthorized notice: empty unified message origin",
            )
            return

        async with self._notification_lock:
            if self._terminated:
                return
            if unified_msg_origin in self._notified_unified_msg_origins:
                return
            sent = False
            recall: RecallCallback | None = None
            onebot_result: tuple[bool, RecallCallback | None] | None = None
            telegram_result: tuple[bool, RecallCallback | None] | None = None
            try:
                onebot_result = await self._try_send_onebot_unauthorized_notice(event)
                if onebot_result is not None:
                    sent, recall = onebot_result
                else:
                    telegram_result = await self._try_send_telegram_unauthorized_notice(
                        event,
                    )
                    if telegram_result is not None:
                        sent, recall = telegram_result
                if onebot_result is None and telegram_result is None:
                    await event.send(
                        MessageChain().message(self.unauthorized_notice_message),
                    )
                    sent = True
                    if self.unauthorized_notice_recall_delay_seconds > 0:
                        logger.debug(
                            "[llm_acl] unauthorized notice auto recall is unavailable for this adapter",
                        )
            except Exception:
                logger.warning(
                    "[llm_acl] failed to send unauthorized notice: %s",
                    unified_msg_origin,
                    exc_info=True,
                )
                return
            if not sent:
                return
            self._notified_unified_msg_origins.add(unified_msg_origin)
            if recall is not None:
                self._schedule_notice_recall(recall)

    async def _block_unauthorized_llm_request(
        self,
        event: AstrMessageEvent,
        hook_name: str,
    ) -> None:
        if self._is_llm_enabled_for_event(event):
            return

        event.stop_event()
        await self._notify_unauthorized_session_once(event)
        target_ids = self._target_ids(event)
        logger.info(
            f"[llm_acl] blocked {hook_name} (acl ids): {', '.join(target_ids)}",
        )

    @filter.on_llm_request(priority=1)
    async def guard_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        await self._block_unauthorized_llm_request(event, "llm request")

    async def terminate(self) -> None:
        async with self._notification_lock:
            self._terminated = True
            tasks = tuple(self._recall_tasks)
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._notification_lock:
            self._recall_tasks.clear()

    @filter.command_group("llmacl")
    def llmacl_group(self):
        """LLM ACL 管理命令组。"""

    @llmacl_group.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        enabled = self._is_llm_enabled_for_event(event)
        yield event.plain_result(
            "\n".join(
                [
                    self._current_scope_label(event),
                    f"当前 LLM: {'开启' if enabled else '关闭'}",
                    f"群聊默认策略: {'开启' if self.default_group_enabled else '关闭'}",
                    f"私聊默认策略: {'开启' if self.default_private_enabled else '关闭'}",
                    f"黑名单数量: {len(self.black_ids)}",
                    f"白名单数量: {len(self.white_ids)}",
                ]
            )
        )

    @llmacl_group.command("id")
    async def cmd_id(self, event: AstrMessageEvent):
        yield event.plain_result(self._current_scope_label(event))

    @llmacl_group.command("sid")
    async def cmd_sid(self, event: AstrMessageEvent):
        yield event.plain_result(
            "会话ID已不再作为名单依据，仅供排查使用。\n"
            f"会话ID: {event.unified_msg_origin}\n"
            f"{self._current_scope_label(event)}"
        )

    @llmacl_group.command("default")
    async def cmd_default(self, event: AstrMessageEvent, target: str = "", mode: str = ""):
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可操作。")
            return

        t = target.strip().lower()
        m = mode.strip().lower()

        # 兼容简易指令 /llmacl default on|off (默认操作当前环境对应的群聊或私聊)
        if t in {"on", "off"} and not m:
            is_group = bool(str(event.get_group_id() or "").strip())
            new_val = t == "on"
            if is_group:
                self.default_group_enabled = new_val
                target_desc = "群聊默认策略"
            else:
                self.default_private_enabled = new_val
                target_desc = "私聊默认策略"
            self._save()
            yield event.plain_result(f"{target_desc}已设置为: {'开启' if new_val else '关闭'}")
            return

        if t not in {"group", "private"} or m not in {"on", "off"}:
            yield event.plain_result("用法:\n/llmacl default group on|off\n/llmacl default private on|off\n/llmacl default on|off (针对当前群聊/私聊环境)")
            return

        new_val = m == "on"
        if t == "group":
            self.default_group_enabled = new_val
            target_desc = "群聊默认策略"
        else:
            self.default_private_enabled = new_val
            target_desc = "私聊默认策略"

        self._save()
        yield event.plain_result(f"{target_desc}已设置为: {'开启' if new_val else '关闭'}")

    @llmacl_group.command("black_add")
    async def cmd_black_add(self, event: AstrMessageEvent, target_id: str = ""):
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可操作。")
            return
        tid = target_id.strip() or self._default_target_id(event)
        if not tid:
            yield event.plain_result("无法获取当前群组或用户 ID，请手动传入 ID。")
            return
        self.black_ids.add(tid)
        self.white_ids.discard(tid)
        self._save()
        yield event.plain_result(f"已加入黑名单并关闭 LLM: {tid}")

    @llmacl_group.command("black_del")
    async def cmd_black_del(self, event: AstrMessageEvent, target_id: str = ""):
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可操作。")
            return
        tid = target_id.strip() or self._default_target_id(event)
        if not tid:
            yield event.plain_result("无法获取当前群组或用户 ID，请手动传入 ID。")
            return
        existed = tid in self.black_ids
        self.black_ids.discard(tid)
        self._save()
        yield event.plain_result(("已移出黑名单: " if existed else "该 ID 不在黑名单: ") + tid)

    @llmacl_group.command("white_add")
    async def cmd_white_add(self, event: AstrMessageEvent, target_id: str = ""):
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可操作。")
            return
        tid = target_id.strip() or self._default_target_id(event)
        if not tid:
            yield event.plain_result("无法获取当前群组或用户 ID，请手动传入 ID。")
            return
        self.white_ids.add(tid)
        self.black_ids.discard(tid)
        self._save()
        yield event.plain_result(f"已加入白名单并开启 LLM: {tid}")

    @llmacl_group.command("white_del")
    async def cmd_white_del(self, event: AstrMessageEvent, target_id: str = ""):
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可操作。")
            return
        tid = target_id.strip() or self._default_target_id(event)
        if not tid:
            yield event.plain_result("无法获取当前群组或用户 ID，请手动传入 ID。")
            return
        existed = tid in self.white_ids
        self.white_ids.discard(tid)
        self._save()
        yield event.plain_result(("已移出白名单: " if existed else "该 ID 不在白名单: ") + tid)

    @llmacl_group.command("list")
    async def cmd_list(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可操作。")
            return
        black = "\n".join(sorted(self.black_ids)) if self.black_ids else "(空)"
        white = "\n".join(sorted(self.white_ids)) if self.white_ids else "(空)"
        yield event.plain_result(f"[黑名单]\n{black}\n\n[白名单]\n{white}")