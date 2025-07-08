import random
import string
from typing import Optional

from gsuid_core.global_val import (
    get_all_bot_dict,
)
from gsuid_core.utils.database.global_val_models import (
    CoreDataSummary,
    CoreDataAnalysis,
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


async def get_detail_chart(bot_id: Optional[str], bot_self_id: Optional[str]):
    characters = string.ascii_lowercase + string.digits
    random_string = ''.join(random.choice(characters) for _ in range(12))
    _p = []
    sum_data = await CoreDataSummary.get_distinct_date_data()
    if sum_data:
        op = [
            {"label": i.strftime('%Y-%m-%d'), "value": i.strftime('%Y-%m-%d')}
            for i in sum_data
        ]
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
                            },
                            {
                                "componentId": f"u:{random_string}2",
                                "ignoreError": False,
                                "actionType": "reload",
                                "data": {
                                    "name": "${event.data.value}",
                                },
                                "dataMergeMode": "merge",
                            },
                            {
                                "componentId": f"u:{random_string}3",
                                "ignoreError": False,
                                "actionType": "reload",
                                "data": {
                                    "name": "${event.data.value}",
                                },
                                "dataMergeMode": "merge",
                            },
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
            "height": "1000px",
            "api": {
                "url": f'/genshinuid/api/loadData1/{bot_id}/{bot_self_id}',
                "method": "post",
                "requestAdaptor": "",
                "adaptor": "",
                "messages": {},
                "dataType": "json",
            },
        }
    )
    _p.append(
        {
            "id": f"u:{random_string}2",
            "type": "chart",
            "replaceChartOption": True,
            "height": "1000px",
            "api": {
                "url": f'/genshinuid/api/loadData2/{bot_id}/{bot_self_id}',
                "method": "post",
                "requestAdaptor": "",
                "adaptor": "",
                "messages": {},
                "dataType": "json",
            },
        }
    )
    _p.append(
        {
            "id": f"u:{random_string}3",
            "type": "chart",
            "replaceChartOption": True,
            "height": "1000px",
            "api": {
                "url": f'/genshinuid/api/loadData3/{bot_id}/{bot_self_id}',
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
    all_bot = {'汇总': ['汇总']}
    all_bot.update(await get_all_bot_dict())
    tabs = []
    for bot_id in all_bot:
        for bot_self_id in all_bot[bot_id]:
            if bot_id == '汇总':
                _bot_id = None
            else:
                _bot_id = bot_id

            if bot_self_id == '汇总':
                _bot_self_id = None
            else:
                _bot_self_id = bot_self_id

            data = await CoreDataAnalysis.calculate_dashboard_metrics(
                _bot_id,
                _bot_self_id,
            )
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
                                get_chart(f'{AAPI}/{_bot_id}/{_bot_self_id}'),
                                get_divider(),
                                get_chart(f'{BAPI}/{_bot_id}/{_bot_self_id}'),
                                get_divider(),
                                *(
                                    await get_detail_chart(
                                        _bot_id, _bot_self_id
                                    )
                                ),
                            ],
                        ),
                    ],
                )
            )
    page['body'].append(get_tabs(tabs))
    return page
