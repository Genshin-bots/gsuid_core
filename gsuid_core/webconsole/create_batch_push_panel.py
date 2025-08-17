from gsuid_core.gss import gss
from gsuid_core.webconsole.create_base_panel import get_alert
from gsuid_core.utils.database.models import CoreUser, CoreGroup

PLUGINS = 'image,lists,preview,link,advlist,wordcount'
TOOLBAR = 'undo redo bold bullist numlist template link image preview'


async def get_batch_push_panel():
    all_user = await CoreUser.get_all_user()
    all_group = await CoreGroup.get_all_group()

    _options_user = []
    _options_group = []

    if all_user:
        for user in all_user:
            uuid = f'u:{user.user_id}|{user.bot_id}'
            u = {"label": f'用户:{user.user_id}', "value": uuid}
            if u not in _options_user:
                _options_user.append(u)

    if all_group:
        for group in all_group:
            uuid = f'g:{group.group_id}|{group.bot_id}'
            g = {"label": f'群聊:{group.group_id}', "value": uuid}
            if g not in _options_group:
                _options_group.append(g)

    options = [
        {"label": "私聊全部", "value": "ALLUSER"},
        {"label": "群聊全部", "value": "ALLGROUP"},
    ]
    bots = [{"label": b, "value": b} for b in gss.active_bot]
    is_disable = False if bots else True

    options.extend(_options_group)
    options.extend(_options_user)

    if is_disable:
        body = [
            get_alert("请先连接Bot, 方可使用该功能!", "warning"),
        ]
    else:
        body = []

    body.extend(
        [
            {
                "type": "input-tag",
                "label": "推送Bot",
                "name": "push_bot",
                "options": bots,
                "optionsTip": "最近您使用的标签",
                "clearable": True,
                "disabled": is_disable,
            },
            {
                "type": "select",
                "label": "推送对象",
                "name": "push_tag",
                "options": options,
                "optionsTip": "连接到的Bot",
                "multiple": True,
                "clearable": True,
                "searchable": True,
                "disabled": is_disable,
            },
            {
                "type": "input-rich-text",
                "name": "push_text",
                "label": "推送文本",
                "receiver": "",
                "vendor": "tinymce",
                "options": {
                    "menubar": True,
                    "plugins": PLUGINS,
                    "toolbar": TOOLBAR,
                },
                "disabled": is_disable,
            },
        ]
    )

    return {
        "type": "page",
        "body": [
            {
                "type": "form",
                "api": "/genshinuid/api/BatchPush",
                "body": body,
                "id": "u:623947e12949",
                "actions": [
                    {
                        "type": "submit",
                        "label": "提交",
                        "primary": True,
                        "id": "u:fa297dc16334",
                    }
                ],
                "feat": "Insert",
            }
        ],
        "id": "u:efc9400121dd",
    }
