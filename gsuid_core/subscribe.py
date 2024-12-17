from typing import Dict, List, Union, Literal, Optional

from gsuid_core.models import Event
from gsuid_core.utils.database.models import Subscribe


class GsCoreSubscribe:
    async def add_subscribe(
        self,
        subscribe_type: Literal['session', 'single'],
        task_name: str,
        event: Event,
        extra_message: Optional[str] = None,
    ):
        '''📝简单介绍:

            该方法允许向数据库添加一个订阅信息的持久化保存
            注意`subscribe_type`参数必须为`session`或`single`
            `session`模式下, 订阅都将在每个有效的session(group或direct)内独立存在 (公告推送)
            `single`模式下, 同个session(group)可能同时存在多个订阅 (签到任务)

        🌱参数:

            🔹subscribe_type (`Literal['session', 'single']`):
                    'session'模式: 同个group/user下只存在一条订阅
                    'single'模式: 同个group下存在多条订阅, 同个user只存在一条订阅

            🔹task_name (`str`):
                    订阅名称

            🔹event (`Event`):
                    事件Event

            🔹extra_message (`Optional[str]`, 默认是 `None`):
                    额外想要保存的信息, 例如推送信息或者数值阈值

        🚀使用范例:

            `await GsCoreSubscribe.add_subscribe('single', '签到', event)`
        '''
        opt: Dict[str, Union[str, int, None]] = {
            'bot_id': event.bot_id,
            'task_name': task_name,
        }
        if subscribe_type == 'session' and event.user_type == 'group':
            opt['group_id'] = event.group_id
            opt['user_type'] = event.user_type
        else:
            opt['user_id'] = event.user_id

        condi = await Subscribe.data_exist(
            **opt,
        )

        if not condi:
            await Subscribe.full_insert_data(
                user_id=event.user_id,
                bot_id=event.bot_id,
                group_id=event.group_id,
                task_name=task_name,
                bot_self_id=event.bot_self_id,
                user_type=event.user_type,
                extra_message=extra_message,
            )
        else:
            upd = {}
            for i in [
                'user_id',
                'bot_id',
                'group_id',
                'bot_self_id',
                'user_type',
            ]:
                if i not in opt:
                    upd[i] = event.__getattribute__(i)

            upd['extra_message'] = extra_message
            await Subscribe.update_data_by_data(
                opt,
                upd,
            )

    async def get_subscribe(self, task_name: str):
        all_data: Optional[List[Subscribe]] = await Subscribe.select_rows(
            task_name=task_name
        )
        return all_data

    async def delete_subscribe(
        self,
        subscribe_type: Literal['session', 'single'],
        task_name: str,
        event: Event,
    ):
        if subscribe_type == 'session' and event.user_type == 'group':
            await Subscribe.delete_row(
                group_id=event.group_id,
                task_name=task_name,
            )
        else:
            await Subscribe.delete_row(
                user_id=event.user_id,
                task_name=task_name,
            )


gs_subscribe = GsCoreSubscribe()
