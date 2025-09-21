from typing import (
    Dict,
    List,
    Union,
    Literal,
    Callable,
    Optional,
    Sequence,
    Awaitable,
    TypedDict,
)

from gsuid_core.models import Event, Message
from gsuid_core.segment import MessageSegment
from gsuid_core.utils.database.models import Subscribe


class GsCoreSubscribe:
    async def add_subscribe(
        self,
        subscribe_type: Literal['session', 'single'],
        task_name: str,
        event: Event,
        extra_message: Optional[str] = None,
        uid: Optional[str] = None,
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
            'uid': uid,
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
                WS_BOT_ID=event.WS_BOT_ID,
                uid=uid,
            )
        else:
            upd = {}
            for i in [
                'user_id',
                'bot_id',
                'group_id',
                'bot_self_id',
                'user_type',
                'WS_BOT_ID',
            ]:
                if i not in opt:
                    upd[i] = event.__getattribute__(i)

            upd['extra_message'] = extra_message
            await Subscribe.update_data_by_data(
                opt,
                upd,
            )

    async def get_subscribe(
        self,
        task_name: str,
        user_id: Optional[str] = None,
        bot_id: Optional[str] = None,
        user_type: Optional[str] = None,
        uid: Optional[str] = None,
        WS_BOT_ID: Optional[str] = None,
    ):
        params = {
            'task_name': task_name,
        }

        if user_id and bot_id and user_type:
            params['user_id'] = user_id
            params['bot_id'] = bot_id
            params['user_type'] = user_type

        if uid:
            params['uid'] = uid

        if WS_BOT_ID:
            params['WS_BOT_ID'] = WS_BOT_ID

        all_data: Optional[Sequence[Subscribe]] = await Subscribe.select_rows(
            distinct=False, **params
        )
        return all_data

    async def delete_subscribe(
        self,
        subscribe_type: Literal['session', 'single'],
        task_name: str,
        event: Event,
        uid: Optional[str] = None,
        WS_BOT_ID: Optional[str] = None,
    ):
        params = {
            'task_name': task_name,
        }
        if uid:
            params['uid'] = uid

        if WS_BOT_ID:
            params['WS_BOT_ID'] = WS_BOT_ID

        if subscribe_type == 'session' and event.user_type == 'group':
            await Subscribe.delete_row(group_id=event.group_id, **params)
        else:
            await Subscribe.delete_row(user_id=event.user_id, **params)

    async def update_subscribe_message(
        self,
        subscribe_type: Literal['session', 'single'],
        task_name: str,
        event: Event,
        extra_message: str,
        uid: Optional[str] = None,
    ):
        sed = {}
        upd = {}

        for i in [
            'bot_id',
            'bot_self_id',
            'user_type',
            'WS_BOT_ID',
        ]:
            sed[i] = event.__getattribute__(i)

        if subscribe_type == 'session' and event.user_type == 'group':
            sed['group_id'] = event.group_id
        else:
            sed['user_id'] = event.user_id

        if uid:
            sed['uid'] = uid

        sed['task_name'] = task_name
        upd['extra_message'] = extra_message

        await Subscribe.update_data_by_data(sed, upd)

    async def muti_task(
        self,
        datas: Sequence[Subscribe],
        func: Callable[..., Awaitable[str]],
        attr: str = 'uid',
    ):
        priv_result: Dict[str, PrivTask] = {}
        group_result: Dict[str, GroupTask] = {}
        for data in datas:
            attr_data = data.__getattribute__(attr)
            if attr_data:
                im = await func(attr_data)
                if data.user_type == 'group':
                    sid = f'{data.WS_BOT_ID}_{data.group_id}'
                    if sid not in group_result:
                        group_result[sid] = {
                            'success': 0,
                            'fail': 0,
                            'event': data,
                            'push_message': [],
                        }
                    if '失败' in im:
                        group_result[sid]['fail'] += 1
                        qid = attr_data = data.__getattribute__("user_id")
                        group_result[sid]['push_message'].extend(
                            [
                                MessageSegment.text('\n'),
                                MessageSegment.at(qid),
                                MessageSegment.text('\n'),
                                MessageSegment.text(im),
                            ]
                        )
                    else:
                        group_result[sid]['success'] += 1
                else:
                    sid = f'{data.WS_BOT_ID}_{data.user_id}'
                    if sid not in priv_result:
                        priv_result[sid] = {
                            'im': [],
                            'event': data,
                            'push_message': [],
                        }
                    priv_result[sid]['im'].append(im)
        return priv_result, group_result

    async def _to_dict(
        self, data: Sequence[Subscribe]
    ) -> Dict[str, List[Subscribe]]:
        result: Dict[str, List[Subscribe]] = {}
        for item in data:
            if str(item.uid) not in result:
                result[str(item.uid)] = []
            result[str(item.uid)].append(item)
        return result


class GroupTask(TypedDict):
    success: int
    fail: int
    event: Subscribe
    push_message: List[Message]


class PrivTask(TypedDict):
    im: List[str]
    event: Subscribe
    push_message: List[Message]


gs_subscribe = GsCoreSubscribe()
