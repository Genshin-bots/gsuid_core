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

    options.extend(_options_group)
    options.extend(_options_user)

    return {
        "type": "page",
        "body": [
            {
                "type": "form",
                "api": "/genshinuid/api/BatchPush",
                "body": [
                    {
                        "type": "input-tag",
                        "label": "推送对象",
                        "name": "push_tag",
                        "options": options,
                        "id": "u:1006b95ebebc",
                        "optionsTip": "最近您使用的标签",
                        "clearable": True,
                    },
                    {
                        "type": "input-rich-text",
                        "name": "push_text",
                        "label": "推送文本",
                        "receiver": "",
                        "id": "u:36619f16e069",
                        "vendor": "tinymce",
                        "options": {
                            "menubar": True,
                            "plugins": PLUGINS,
                            "toolbar": TOOLBAR,
                        },
                    },
                ],
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
