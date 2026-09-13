# astrbot_plugin_llm_session_acl

管理员可直接配置群组 ID 或用户 ID 黑白名单，控制指定群聊或指定私聊用户是否允许调用 LLM。

## 规则优先级

1. `black_ids`：群聊的群组 ID 或私聊的用户 ID 命中时强制关闭。
2. `white_ids`：群聊的群组 ID 或私聊的用户 ID 命中时强制开启。
3. 其他消息：按场景默认策略判定（群聊走 `default_group_enabled`，私聊走 `default_private_enabled`）。

黑名单优先级高于白名单。群聊消息只检查群组 ID；私聊消息检查发送者用户 ID。

## 配置项

- `admin_users`：额外允许操作 `/llmacl` 管理命令的用户 ID。
- `default_group_enabled`：群聊默认是否开启 LLM（默认 `true`）。
- `default_private_enabled`：私聊默认是否开启 LLM（默认 `true`）。
- `unauthorized_notice_enabled`：是否向被 ACL 拒绝的会话发送提示。关闭后仍会阻止请求。
- `unauthorized_notice_message`：提示消息内容。留空时不发送提示。
- `unauthorized_notice_recall_delay_seconds`：提示自动撤回延时，单位为秒。`0` 为关闭；正整数启用自动撤回。
- `black_ids`：黑名单，直接填写群组 ID 或用户 ID。
- `white_ids`：白名单，直接填写群组 ID 或用户 ID。

旧版 `black_sessions` / `white_sessions` 与 `default_enabled` 会在插件启动时兼容读取，但保存后会写入新配置字段。

## 未授权提示

当 ACL 判定为关闭 LLM（黑名单命中，或未命中白名单且对应场景默认策略为关闭）时，插件会阻止该次请求。若启用了 `unauthorized_notice_enabled` 且 `unauthorized_notice_message` 非空，会向该会话发送一次提示。

去重仅在插件运行期间生效，按完整的 `event.unified_msg_origin` 记录。仅在消息发送成功后才标记为已提示；发送失败时会在后续请求重试。重载或重启插件后，该会话首次再次被拒绝时会重新提示。

自动撤回当前可靠支持 OneBot/aiocqhttp 与 Telegram。OneBot 群聊和私聊、Telegram 群聊和私聊（包括 `群组ID#话题ID`）会在配置的延时后撤回提示；其他平台会正常保留提示，但不会自动撤回。

## 命令

- `/llmacl id` 查看当前群组 ID / 用户 ID。
- `/llmacl sid` 查看旧会话 ID 与当前群组 ID / 用户 ID，仅用于排查。
- `/llmacl status` 查看当前消息命中的 LLM 状态及群聊/私聊默认策略。
- `/llmacl default group on|off` 设置群聊默认策略（管理员）。
- `/llmacl default private on|off` 设置私聊默认策略（管理员）。
- `/llmacl default on|off` 快捷设置当前环境（群聊或私聊）的默认策略（管理员）。
- `/llmacl black_add [id]` 加入黑名单（管理员）。
- `/llmacl black_del [id]` 移出黑名单（管理员）。
- `/llmacl white_add [id]` 加入白名单（管理员）。
- `/llmacl white_del [id]` 移出白名单（管理员）。
- `/llmacl list` 查看黑白名单（管理员）。

不传 `id` 时，群聊默认操作当前群组 ID，私聊默认操作当前用户 ID。