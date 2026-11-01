"""会话管理（消息记录）的段序列化与实时广播。

这两个模块是控制台那条链路的契约层：段结构变了前端就渲染不出来，广播丢了页面就只剩
手动刷新。所以这里只测「形状」与「能不能送到」，不测 HTTP。
"""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from gsuid_core.models import Event, Message
from gsuid_core.message_history.hub import HistoryHub
from gsuid_core.message_history.segments import (
    segments_preview,
    segments_from_event,
    segments_from_messages,
    chat_message_from_record,
)


def test_text_at_and_unknown_segments():
    segments = segments_from_messages([Message("text", "你好"), Message("at", "10000"), Message("weird_type", "x")])
    assert segments[0] == {"type": "text", "text": "你好"}
    assert segments[1]["type"] == "at"
    assert segments[1]["qq"] == "10000"
    assert segments[2]["type"] == "unknown"
    assert segments[2]["kind"] == "weird_type"


def test_file_segment_keeps_name_only():
    """文件段只留文件名。

    `MessageSegment.file` 产出的是 `文件名|<裸base64>`，但收到的文件本来就是 url —— 控制台知道「发过这个文件」就够了。
    """
    from gsuid_core.segment import MessageSegment

    seg = segments_from_messages([MessageSegment.file(b"hello world", "a.txt")])[0]

    assert seg == {"type": "file", "name": "a.txt", "url": ""}


def test_media_never_enters_memory(tmp_path):
    """音视频在协议端只有直链或 base64，本地路径一律不碰、不存。

    以前这里会把路径字符串登记进 RM；现在连字符串都不留，只给类型占位。
    """
    from gsuid_core.utils.resource_manager import RM

    before = RM.resource_count
    secret = tmp_path / "secret.mp4"
    secret.write_bytes(b"SECRET-BYTES")

    seg = segments_from_messages([Message("video", str(secret))])[0]

    assert seg == {"type": "video", "url": ""}
    assert RM.resource_count == before


def test_image_local_path_is_shown_as_is(tmp_path):
    """图片也不进内存：本地路径原样交给前端显示，不读盘、不登记 RM。"""
    from gsuid_core.utils.resource_manager import RM

    before = RM.resource_count
    image = tmp_path / "a.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    seg = segments_from_messages([Message("image", str(image))])[0]

    assert seg == {"type": "image", "url": "", "name": str(image)}
    assert RM.resource_count == before


def test_resource_route_never_serves_html():
    """资源接口吐的是消息里的原始字节，绝不能回 `text/html`。

    这条路由和面板同源，`data:text/html` 原样回出去就是可执行脚本。
    """
    import base64

    from gsuid_core.utils.resource_manager import RM
    from gsuid_core.webconsole.history_api import get_history_resource

    payload = base64.b64encode(b"<script>alert(document.cookie)</script>").decode()
    rid = RM.register(f"data:text/html;base64,{payload}")
    resp = asyncio.run(get_history_resource(rid))

    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_resource_route_uses_real_media_type():
    """视频曾经被一律当 `image/png` 返回，`<video>` 直接不播。"""
    from gsuid_core.utils.resource_manager import RM
    from gsuid_core.webconsole.history_api import get_history_resource

    mp4 = RM.register_video(b"\x00\x00\x00\x18ftypisom" + b"\0" * 16)
    png = RM.register(b"\x89PNG\r\n\x1a\n" + b"\0" * 16)

    assert asyncio.run(get_history_resource(mp4)).headers["content-type"] == "video/mp4"
    assert asyncio.run(get_history_resource(png)).headers["content-type"] == "image/png"


def test_card_only_message_is_recorded(monkeypatch):
    """纯卡片消息没有正文，早退条件会把它整条丢掉，控制台的会话记录里就「收不到」卡片。

    官机来的卡片就是这种形状：`content=[Message(type='card', ...)]`，raw_text 为空。
    """
    from gsuid_core import handler as handler_module

    recorded: list[dict] = []
    monkeypatch.setattr(
        handler_module,
        "get_history_manager",
        lambda: SimpleNamespace(add_message=lambda **kwargs: recorded.append(kwargs)),
    )

    event = Event(bot_id="QQBotUID", bot_self_id="4015131812", user_id="OPENID", user_type="direct")
    event.content = [Message("card", {"type": "tuwen", "content": {"title": "图文卡片测试", "desc": "说明"}})]
    handler_module._record_inbound_history(event)

    assert recorded, "纯卡片消息必须入库，否则控制台看不到"
    # 段数组走独立参数，不能塞进 metadata —— 那是内存里给 AI 的那份
    assert "segments" not in recorded[0]["metadata"]
    assert recorded[0]["segments"][0]["type"] == "ark"


def test_plain_text_is_not_parsed_as_markdown():
    """普通文本原样保留，哪怕里面长得像 markdown。

    正文是用户可控的，一旦在这里解析，`![x](D:/bot/config.py)` 就会把服务器上的文件读进内存。
    只有平台/插件产出的 `markdown` 段才走解析。
    """
    for text in ("看图 ![图](https://example.com/a.png) 就好", "![x](D:/bot/config.py)"):
        segments = segments_from_messages([Message("text", text)])
        assert segments[0] == {"type": "text", "text": text}
        assert "/api/history/resource/" not in text


def test_plain_chat_text_stays_text():
    # 普通聊天里的算术、下划线、星号不能被当成 markdown
    for text in ("1+1=2", "a_b_c", "价格是 100 元", "*不是斜体*"):
        segments = segments_from_messages([Message("text", text)])
        assert segments[0] == {"type": "text", "text": text}


def test_markdown_keeps_local_image_path():
    """markdown 里的本地图片不再改写，原样保留路径 —— 内容不进内存。"""
    segments = segments_from_messages([Message("markdown", "![图标](D:/bot/a.png)")])
    assert segments[0]["content"] == "![图标](D:/bot/a.png)"


def test_markdown_keeps_remote_image():
    segments = segments_from_messages([Message("markdown", "![图标](https://example.com/a.png)")])
    assert segments[0]["content"] == "![图标](https://example.com/a.png)"


def test_preview_strips_markdown_syntax():
    segments = segments_from_messages(
        [Message("markdown", "![图](https://example.com/a.png) [查询](mqqapi://aio/inlinecmd?command=x)\n> 提示")]
    )
    preview = segments_preview(segments)
    assert "![" not in preview
    assert "https://" not in preview
    assert "mqqapi" not in preview
    assert "查询" in preview


def test_image_keeps_direct_link():
    segments = segments_from_messages([Message("image", "link://https://example.com/a.png")])
    assert segments[0] == {"type": "image", "url": "https://example.com/a.png"}


def test_image_without_source_degrades_to_error():
    # 空串既不是直链也没有路径可显示，降级成一条带 error 的图片段，前端显示占位
    segments = segments_from_messages([Message("image", "")])
    assert segments[0]["type"] == "image"
    assert segments[0]["url"] == ""
    assert segments[0]["error"]


def test_markdown_and_buttons():
    segments = segments_from_messages(
        [
            Message("markdown", "## 标题"),
            Message("buttons", [[{"text": "点我", "callback": "hi"}]]),
        ]
    )
    assert segments[0] == {"type": "markdown", "content": "## 标题"}
    assert segments[1]["type"] == "button"
    assert segments[1]["rows"][0][0]["text"] == "点我"
    assert segments[1]["rows"][0][0]["callback"] == "hi"


def test_node_becomes_forward_with_items():
    segments = segments_from_messages([Message("node", [Message("text", "甲"), Message("text", "乙")])])
    assert segments[0]["type"] == "forward"
    assert segments[0]["count"] == 2
    assert segments[0]["items"][0][0]["text"] == "甲"


def test_node_depth_limit_keeps_only_count():
    deep = Message("node", [Message("text", "叶子")])
    for _ in range(5):
        deep = Message("node", [deep])
    segments = segments_from_messages([deep])

    # 一路往下走到最深的那层转发，超过上限后子项为空、只留条数
    node = segments[0]
    depth = 0
    while node["type"] == "forward" and node["items"]:
        node = node["items"][0][0]
        depth += 1
    assert node["type"] == "forward"
    assert node["items"] == []
    assert depth <= 3


def test_file_segment_splits_name_and_payload():
    segments = segments_from_messages([Message("file", "报告.pdf|link://https://example.com/r.pdf")])
    assert segments[0]["type"] == "file"
    assert segments[0]["name"] == "报告.pdf"
    assert segments[0]["url"] == "https://example.com/r.pdf"


def test_image_size_is_dropped():
    # 只影响出图排版，不该占一条可见消息
    assert segments_from_messages([Message("image_size", (1, 1))]) == []


def test_segments_from_event_prefers_content():
    event = Event(bot_id="b", bot_self_id="1", group_id="2", user_id="3", user_type="group")
    event.raw_text = "兜底文本"
    event.content = [Message("text", "来自 content")]
    assert segments_from_event(event) == [{"type": "text", "text": "来自 content"}]


def test_segments_from_event_falls_back_to_raw_text():
    event = Event(bot_id="b", bot_self_id="1", group_id="2", user_id="3", user_type="group")
    event.raw_text = "兜底文本"
    assert segments_from_event(event) == [{"type": "text", "text": "兜底文本"}]


def test_preview_collapses_media_to_labels():
    text = segments_preview(
        segments_from_messages(
            [
                Message("text", "看图"),
                Message("image", "link://https://example.com/a.png"),
                Message("face", "1"),
            ]
        )
    )
    assert text == "看图 [图片] [表情]"


def test_preview_truncates():
    assert len(segments_preview(segments_from_messages([Message("text", "字" * 200)]), limit=10)) == 10


def test_chat_message_from_record_falls_back_to_content():
    class _Record:
        role = "user"
        content = "只有正文"
        user_id = "777"
        user_name = "小明"
        user_avatar = None
        timestamp = 1.0
        metadata = {}

    item = chat_message_from_record(_Record())
    assert item["segments"] == [{"type": "text", "text": "只有正文"}]
    assert item["sender"] == {}
    assert item["msg_id"] == ""


def test_hub_delivers_from_other_thread():
    async def scenario():
        hub = HistoryHub()
        hub.bind_loop(asyncio.get_running_loop())
        sub_id, queue = hub.subscribe()

        # add_message 可能在线程池里跑，所以 publish 必须能从别的线程投递
        worker = threading.Thread(target=hub.publish, args=({"type": "message"},))
        worker.start()
        worker.join()

        payload = await asyncio.wait_for(queue.get(), timeout=2)
        hub.unsubscribe(sub_id)
        assert payload == {"type": "message"}

    asyncio.run(scenario())


def test_hub_publish_without_subscribers_is_noop():
    hub = HistoryHub()
    hub.publish({"type": "message"})  # 没有循环、没有订阅者，不该抛


def test_card_fields_use_official_names():
    """图文卡片的字段名只认 QQ 官方那套，其余一律丢掉。

    官方接口对多余字段是报错而不是忽略，所以 ``desc`` / ``picUrl`` / ``jumpUrl``
    这些别名不做兼容 —— 留着它们只会在发送时整条 400。
    """
    from gsuid_core.webconsole.session_chat_api import _card_segments

    seg = _card_segments(
        '{"type": "tuwen", "content": '
        '{"title": "标题", "description": "描述", "pic_url": "https://p", "url": "https://u",'
        ' "desc": "别名", "picUrl": "https://x", "jumpUrl": "https://y", "prompt": "x"}}'
    )[0]
    assert seg.type == "card"
    assert seg.data["type"] == "tuwen"
    assert seg.data["content"] == {
        "title": "标题",
        "description": "描述",
        "pic_url": "https://p",
        "url": "https://u",
    }


def test_card_rejects_when_only_aliases_given():
    """只给旧别名时等于一个字段都没填，必须报错而不是发出空卡片。"""
    from gsuid_core.webconsole.session_chat_api import _card_segments

    with pytest.raises(ValueError):
        _card_segments('{"content": {"desc": "描述", "picUrl": "https://p"}}')


def test_button_keeps_style_and_action():
    """style / action 是整数，早先只收字符串会把它们丢掉，前端就只剩一种颜色。"""
    segments = segments_from_messages(
        [
            Message(
                "buttons",
                [
                    [
                        {"text": "1", "data": "1", "style": 1, "action": 2, "permisson": 2},
                        {"text": "33", "data": "33", "style": 4, "action": 2, "permission": 0},
                    ]
                ],
            )
        ]
    )
    first, second = segments[0]["rows"][0]
    assert first == {
        "text": "1",
        "data": "1",
        "style": 1,
        "action": 2,
        "permission": 2,
    }
    # 模型里那个字段名少个 s，输出统一成 permission
    assert second["style"] == 4
    assert second["permission"] == 0


def test_markdown_segment_content_is_plain_text():
    """markdown 段的 data 是 {"content": "..."}，不能直接 str() 成 dict 的 repr。"""
    segments = segments_from_messages([Message("markdown", {"content": "## 标题\n正文"})])
    assert segments == [{"type": "markdown", "content": "## 标题\n正文"}]
    assert segments_preview(segments) == "标题 正文"


def test_rich_only_message_falls_back_to_preview():
    """只有富媒体段、没有配文时，content 要兜底成摘要，否则 AI 上下文里是条空消息。"""
    from gsuid_core.handler import _record_inbound_history
    from gsuid_core.message_history import get_history_manager

    def _event(content, raw_text=""):
        return Event(
            bot_id="onebot",
            real_bot_id="onebot",
            bot_self_id="10000",
            user_id="20000",
            group_id="30000",
            user_type="group",
            content=content,
            raw_text=raw_text,
            sender={"nickname": "测试"},
        )

    ark = _event([Message("ark", {"content": [{"type": "json", "content": {"prompt": "推送"}}]})])
    _record_inbound_history(ark)
    assert get_history_manager().get_history(ark)[-1].content == "[卡片]"

    # 有配文就保持原文，不被摘要顶掉
    mixed = _event(
        [
            Message("text", "看这个"),
            Message("ark", {"content": [{"type": "json", "content": {"prompt": "x"}}]}),
        ],
        raw_text="看这个",
    )
    _record_inbound_history(mixed)
    assert get_history_manager().get_history(mixed)[-1].content == "看这个"


def test_segments_stay_out_of_memory():
    """段数组是原始数据：只随广播和缓存落库，不进内存里的 MessageRecord。"""
    from gsuid_core.message_history import get_history_manager

    manager = get_history_manager()
    event = Event(
        bot_id="onebot",
        real_bot_id="onebot",
        bot_self_id="10000",
        user_id="20000",
        group_id="30000",
        user_type="group",
    )
    segments = [{"type": "text", "text": "你好"}, {"type": "image", "url": "https://x/a.png"}]
    record = manager.add_message(
        event=event,
        role="user",
        content="你好",
        metadata={"sender": {"nickname": "测试"}},
        segments=segments,
    )

    assert "segments" not in record.metadata
    # AI 读的 content 不受影响
    assert record.content == "你好"
    # 内存里取回来只有纯文本兜底，段数组不在
    item = chat_message_from_record(record)
    assert item["segments"] == [{"type": "text", "text": "你好"}]
    # 手上还有段数组的那份（广播 / 落库）仍然是完整的
    assert chat_message_from_record(record, segments)["segments"] == segments


def test_sender_snapshot_keeps_group_name():
    """官机入站会把群名塞进 sender，快照要带着，否则控制台只能显示 openid。"""
    from gsuid_core.handler import _sender_snapshot

    event = Event(
        bot_id="qqgroup",
        real_bot_id="qqgroup",
        bot_self_id="4015131812",
        user_id="1A790910D2752250",
        group_id="F2F64FC101C921F51",
        user_type="group",
        sender={"nickname": "小明", "avatar": "https://x/y.png", "group_name": "摸鱼群"},
    )
    snapshot = _sender_snapshot(event)
    assert snapshot["group_name"] == "摸鱼群"
    assert snapshot["nickname"] == "小明"
    # sender 里没有就别凭空造一个
    event.sender = {"nickname": "小红"}
    assert "group_name" not in _sender_snapshot(event)


def test_sender_snapshot_keeps_group_icon():
    """OneBot 从群资料缓存里补的群头像也要跟着快照走。"""
    from gsuid_core.handler import _sender_snapshot

    event = Event(
        bot_id="onebot",
        real_bot_id="onebot",
        bot_self_id="3584145712",
        user_id="12345",
        group_id="575663150",
        user_type="group",
        sender={
            "nickname": "小红",
            "group_name": "摸鱼群",
            "group_icon": "https://p.qlogo.cn/gh/575663150/575663150/100",
        },
    )
    snapshot = _sender_snapshot(event)
    assert snapshot["group_icon"] == "https://p.qlogo.cn/gh/575663150/575663150/100"


def test_group_title_reads_sender_before_database(monkeypatch):
    """sender 里已经有群名就不查库，缺了才兜底。"""
    from gsuid_core.webconsole import session_chat_api as api

    calls = []

    async def fake_meta(group_id):
        calls.append(group_id)
        return {"group_name": "库里的群名", "group_icon": "https://db/i.png"}

    monkeypatch.setattr(api, "_group_meta", fake_meta)

    got = asyncio.run(
        api._title_avatar(
            True,
            "575663150",
            "1",
            {"group_name": "摸鱼群", "group_icon": "https://x/i.png"},
        )
    )
    assert got == ("摸鱼群", "https://x/i.png")
    assert calls == []

    got = asyncio.run(api._title_avatar(True, "575663150", "1", {}))
    assert got == ("库里的群名", "https://db/i.png")
    assert calls == ["575663150"]


def test_send_result_reads_adapter_error_code():
    """适配器把失败编码成 __send_error__:原因，控制台要认出来，不能一律报成功。"""
    from types import SimpleNamespace

    from gsuid_core.webconsole import session_chat_api as api

    bot = SimpleNamespace(_supports_recall=True)

    # 正常拿到消息 id
    assert api._send_error(["123456"], bot) == ""
    # 适配器带回错误码
    assert api._send_error(["__send_error__:机器人不在该群"], bot) == "机器人不在该群"
    assert api._send_error(["__send_error__:"], bot) == "发送失败"
    # 一帧回执都没有 = 没发出去
    assert api._send_error([], bot)
    # 老适配器压根不支持回执，判失败会误报，放行
    assert api._send_error([], SimpleNamespace(_supports_recall=False)) == ""


def test_merge_session_sorts_by_timestamp():
    """merge_session 把 source 合并到 target 时必须按时间戳严格递增排序。"""
    from gsuid_core.message_history import get_history_manager

    manager = get_history_manager()
    ev_source = Event(bot_id="onebot", WS_BOT_ID="ws1", bot_self_id="", group_id="123", user_type="group")
    ev_target = Event(bot_id="onebot", WS_BOT_ID="ws1", bot_self_id="bot1", group_id="123", user_type="group")

    manager.delete_session(ev_source)
    manager.delete_session(ev_target)

    r_target = manager.add_message(ev_target, role="user", content="第二条")
    r_target.timestamp = 200.0
    r_source = manager.add_message(ev_source, role="user", content="第一条")
    r_source.timestamp = 100.0

    assert manager.merge_session(ev_source, ev_target) is True
    records = manager.get_history(ev_target)
    assert len(records) == 2
    assert [r.content for r in records] == ["第一条", "第二条"]
    assert records[0].timestamp < records[1].timestamp
