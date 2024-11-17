from gsuid_core.global_val import get_all_bot_dict, get_global_analysis
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


async def get_analysis_page():
    AAPI = '/genshinuid/api/getAnalysisData'
    BAPI = '/genshinuid/api/getAnalysisUserGroup'
    page = {
        'type': 'page',
        'title': '数据分析',
        'body': [],
        'id': 'u:a9be7e0dc626',
    }
    all_bot = get_all_bot_dict()
    tabs = []
    for bot_id in all_bot:
        for bot_self_id in all_bot[bot_id]:
            data = await get_global_analysis(bot_id, bot_self_id)
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
                            ],
                        ),
                    ],
                )
            )
    page['body'].append(get_tabs(tabs))
    return page
