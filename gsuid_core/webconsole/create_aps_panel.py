from typing import Union

from gsuid_core.aps import get_all_aps_job, _get_trigger_description


def get_plugin_name(func_ref: str) -> Union[str, None]:
    if "plugins." not in func_ref:
        return None
    parts = func_ref.split("plugins.", 1)[1]
    return parts.split(".", 1)[0] if parts else None


def generate_amis_job_list() -> dict:
    """
    将 APScheduler 的 Job 列表转换为 Amis 列表页面所需的 JSON 数据结构。

    Args:
        jobs: get_all_aps_job() 返回的 Job 对象列表。

    Returns:
        一个包含 Amis 列表组件 JSON 配置的字典。
    """

    jobs = get_all_aps_job()
    amis_data_list = []

    for job in jobs:
        if not hasattr(job, '__slots__'):
            job_dict = vars(job)
        else:
            job_dict = {slot: getattr(job, slot) for slot in job.__slots__}

        func_name = job_dict.get('name', 'N/A')
        func_doc: str = job_dict.get('func').__doc__ or '暂无函数描述'

        # 2. 格式化下次运行时间
        next_run = job.next_run_time
        if next_run:
            next_run_str = next_run.strftime('%Y-%m-%d %H:%M:%S')
        else:
            next_run_str = "已完成/已暂停"

        # 3. 提取运行规律描述
        trigger_desc = _get_trigger_description(job.trigger)

        plugin_name = get_plugin_name(job_dict.get('func_ref', ''))
        if plugin_name is None:
            plugin_name = '未知'

        # 4. 组装成 Amis 列表项数据
        amis_item = {
            "id": job.id,
            "func_name": func_name,
            "func_doc": func_doc,
            "trigger_desc": trigger_desc,
            "next_run_time": next_run_str,
            "plugin_name": plugin_name,
        }
        amis_data_list.append(amis_item)

    # 按照plugin_name排序
    amis_data_list.sort(key=lambda x: x['plugin_name'], reverse=True)
    _func_doc = (
        "<div style='max-width:280px;white-space:nowrap;overflow:hidden;"
        "text-overflow:ellipsis;' title='${func_doc}'>${func_doc}</div>"
    )

    # 5. 构造 Amis 列表组件的完整结构
    amis_json = {
        "type": "page",
        "title": "任务调度列表",
        "body": [
            {
                "type": "table",
                "source": "${items}",
                "columnsTogglable": False,
                "footable": True,
                "columns": [
                    {"name": "id", "label": "任务 ID", "type": "text"},
                    {"name": "plugin_name", "label": "插件名", "type": "text"},
                    {"name": "func_name", "label": "函数名", "type": "text"},
                    {
                        "name": "func_doc",
                        "label": "任务说明",
                        "type": "tpl",
                        "tpl": _func_doc,
                    },
                    {
                        "name": "trigger_desc",
                        "label": "运行规律",
                        "type": "text",
                    },
                    {
                        "name": "next_run_time",
                        "label": "下次运行时间",
                        "type": "text",
                    },
                ],
                "data": {"items": amis_data_list},
            },
        ],
    }

    return amis_json
