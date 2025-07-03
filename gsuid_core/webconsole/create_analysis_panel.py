import random
import string

from gsuid_core.global_val import (
    global_val_path,
    get_all_bot_dict,
    get_value_analysis,
    get_global_analysis,
)
from gsuid_core.webconsole.create_base_panel import (
    get_tab,
    get_card,
    get_tabs,
    get_divider,
    get_property,
)


def get_chart(api: str):
    return {
        "type": "chart",
        "api": api,
    }


def get_detail_chart(bot_id: str, bot_self_id: str):
    characters = string.ascii_lowercase + string.digits
    random_string = ''.join(random.choice(characters) for _ in range(12))
    _p = []
    path = global_val_path / bot_id / bot_self_id
    if path.exists():
        op = [{"label": i.name, "value": i.name} for i in path.iterdir()]
        _p.append(
            {
                "type": "select",
                "label": "选择日期",
                "name": "select",
                "options": op,
                "id": "u:4cb4efccc603",
                "multiple": False,
                "onEvent": {
                    "change": {
                        "weight": 0,
                        "actions": [
                            {
                                "componentId": f"u:{random_string}",
                                "ignoreError": False,
                                "actionType": "reload",
                                "data": {
                                    "name": "${event.data.value}",
                                },
                                "dataMergeMode": "merge",
                            }
                        ],
                    }
                },
            }
        )
        _p.append(get_divider())
    _p.append(
        {
            "id": f"u:{random_string}",
            "type": "chart",
            "replaceChartOption": True,
            "api": {
                "url": f'/genshinuid/api/loadData/{bot_id}/{bot_self_id}',
                "method": "post",
                "requestAdaptor": "",
                "adaptor": "",
                "messages": {},
                "dataType": "json",
            },
        }
    )
    return _p


async def get_analysis_page():
    AAPI = '/genshinuid/api/getAnalysisData'
    BAPI = '/genshinuid/api/getAnalysisUserGroup'
    page = {
        'type': 'page',
        'title': '数据分析',
        'body': [],
        'id': 'u:a9be7e0dc626',
    }
    all_bot = await get_all_bot_dict()
    tabs = []
    for bot_id in all_bot:
        for bot_self_id in all_bot[bot_id]:
            now_data, _ = await get_value_analysis(
                bot_id,
                bot_self_id,
                30,
            )
            data = await get_global_analysis(now_data)
            tabs.append(
                get_tab(
                    f'{bot_id}({bot_self_id})',
                    [
                        get_card(
                            f'✨ {bot_self_id}',
                            [
                                get_property(
                                    {
                                        '活跃用户数（DAU）': data['DAU'],
                                        '活跃群组数（DAG）': data['DAG'],
                                        '用户留存': data['OU'],
                                        '用户新增': data['NU'],
                                    },
                                    2,
                                ),
                                get_divider(),
                                get_chart(f'{AAPI}/{bot_id}/{bot_self_id}'),
                                get_divider(),
                                get_chart(f'{BAPI}/{bot_id}/{bot_self_id}'),
                                get_divider(),
                                *get_detail_chart(bot_id, bot_self_id),
                            ],
                        ),
                    ],
                )
            )
    page['body'].append(get_tabs(tabs))
    return page
