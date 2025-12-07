import asyncio
from io import BytesIO
from typing import Dict, List, Optional, Sequence
from pathlib import Path
from datetime import date as dt_date, datetime, timedelta

import aiofiles
from bs4 import Tag, BeautifulSoup
from PIL import Image
from fastapi import UploadFile, BackgroundTasks
from fastapi.responses import Response, StreamingResponse
from starlette.requests import Request
from fastapi.staticfiles import StaticFiles

from gsuid_core.sv import SL
from gsuid_core.gss import gss
from gsuid_core.config import CONFIG_DEFAULT, core_config
from gsuid_core.logger import LOG_PATH, HistoryLogData, read_log
from gsuid_core.segment import Message, MessageSegment
from gsuid_core.data_store import image_res, backup_path
from gsuid_core.webconsole.mount_app import site
from gsuid_core.utils.database.models import CoreUser, CoreGroup
from gsuid_core.utils.backup.backup_core import copy_and_rebase_paths
from gsuid_core.utils.plugins_config.models import (
    GsImageConfig,
    GsListStrConfig,
)
from gsuid_core.utils.plugins_update._plugins import (
    check_status,
    check_plugins,
    install_plugin,
    update_plugins,
    check_can_update,
    get_plugins_list,
)
from gsuid_core.utils.plugins_config.gs_config import (
    backup_config,
    all_config_list,
    core_plugins_config,
)
from gsuid_core.utils.database.global_val_models import (
    DataType,
    CoreDataSummary,
    CoreDataAnalysis,
)

from .app_life import app

is_clean_pic = core_plugins_config.get_config("EnableCleanPicSrv").data
pic_expire_time = core_plugins_config.get_config("ScheduledCleanPicSrv").data

TEMP_DICT: Dict[str, Dict] = {}


@app.post("/genshinuid/downloadBackUp")
@site.auth.requires("root", response=Response(status_code=403))
async def _download_backup(request: Request, data: Dict):
    host = request.headers.get("host")
    scheme = request.url.scheme
    base_url = f"{scheme}://{host}"

    backup_file: str = data["backup_file"]
    backup_path = Path(backup_file)
    if not backup_path.exists():
        return Response(status_code=404)

    DOWNLOAD_API = "/genshinuid/downloadFile"
    download_url = f"{base_url}{DOWNLOAD_API}?file_id={backup_path.name}"

    return {
        "status": 0,
        "msg": "数据提交成功",
        "data": {
            "fileId": backup_path.name,
            "downloadUrl": download_url,
        },
        "redirect": download_url,
        "actions": [
            {
                "actionType": "download",
                "url": download_url,
                "method": "get",
                "blank": True,
            }
        ],
    }


@app.get("/genshinuid/backupFiles")
@site.auth.requires("root", response=Response(status_code=403))
async def _backup_files(request: Request):
    backup_files = [
        {
            "fileName": i.name,
            "downloadUrl": f"/genshinuid/downloadFile?file_id={i.name}",
            "deleteUrl": f"/genshinuid/deleteBackUp?file_id={i.name}",
        }
        for i in backup_path.glob("*.zip")
    ]
    return {
        "status": 0,
        "msg": "数据提交成功",
        "data": {
            "items": backup_files,
        },
    }


@app.get("/genshinuid/backUpNow")
@site.auth.requires("root", response=Response(status_code=403))
async def _back_up_now(request: Request):
    retcode = copy_and_rebase_paths(None, "NowFile")
    if retcode != 0:
        return Response(status_code=500)

    return {
        "status": 0,
        "msg": "成功完成备份!",
    }


@app.get("/genshinuid/deleteBackUp")
@site.auth.requires("root", response=Response(status_code=403))
async def _delete_backup(request: Request):
    file_id = request.query_params.get("file_id")

    if not file_id:
        return Response("缺少文件标识符", status_code=400)

    _path = Path(backup_path / file_id)
    if not _path.exists():
        return Response("文件未找到", status_code=404)

    try:
        _path.unlink()
    except Exception as e:
        return Response(f"删除文件失败: {e}", status_code=500)
    return {
        "status": 0,
        "msg": "成功删除备份文件!",
    }


@app.get("/genshinuid/downloadFile")
@site.auth.requires("root", response=Response(status_code=403))
async def _download_file(request: Request):
    # 从 URL query string 中获取文件 ID
    file_id = request.query_params.get("file_id")

    if not file_id:
        return Response("缺少文件标识符", status_code=400)

    _path = Path(backup_path / file_id)
    if not _path.exists():
        return Response("文件未找到", status_code=404)

    async with aiofiles.open(_path, "rb") as f:
        content = await f.read()

        headers = {"Content-Disposition": f'attachment; filename="{file_id}"'}

        # 返回文件流
        return Response(content, media_type="application/octet-stream", headers=headers)


@app.post("/genshinuid/setBackUp")
@site.auth.requires("root", response=Response(status_code=403))
async def _set_backup(request: Request, data: Dict):
    backup_time: str = data["backup_time"]
    backup_dir: str = data["backup_dir"]
    backup_method: str = data["backup_method"]

    backup_config.set_config("backup_time", backup_time)
    backup_config.set_config("backup_dir", backup_dir.split(","))
    backup_config.set_config("backup_method", backup_method.split(","))

    backup_config.update_config()
    return Response(status_code=200)


@app.post("/genshinuid/uploadImage/{suffix}/{filename}/{UPLOAD_PATH:path}")
@site.auth.requires("root", response=Response(status_code=403))
async def _upload_image(
    request: Request,
    UPLOAD_PATH: str,
    file: UploadFile,
    filename: Optional[str],
    suffix: Optional[str],
):
    path = Path(UPLOAD_PATH)
    # 利用uuid保存图片
    file_name = file.filename
    if not filename:
        if file_name:
            file_name = file_name.split(".")[-1]
            file_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}.{file_name}"
        else:
            file_name = "image.jpg"
    else:
        if suffix:
            file_name = f"{filename}.{suffix}"
        else:
            file_name = f"{filename}.jpg"

    file_path = path / file_name
    if not file_path.parent.exists():
        file_path.parent.mkdir(parents=True)
    async with aiofiles.open(file_path, "wb") as f:
        content = await file.read()
        await f.write(content)


@app.get("/genshinuid/getImage/{suffix}/{filename}/{IMAGE_PATH:path}")
@site.auth.requires("root", response=Response(status_code=403))
async def _get_image(
    request: Request,
    IMAGE_PATH: str,
    filename: str,
    suffix: str = "str",
):
    path = Path(IMAGE_PATH)
    file_path = path / f"{filename}.{suffix}"
    if not file_path.exists():
        return Response(status_code=404)

    # 返回URL
    return Response(
        content=file_path.read_bytes(),
        media_type="image/jpeg",
    )


@app.post("/genshinuid/setSV/{name}")
@site.auth.requires("root")
def _set_SV(request: Request, data: Dict, name: str):
    if name in SL.lst:
        sv = SL.lst[name]
        data["pm"] = int(data["pm"])
        data["priority"] = int(data["priority"])

        data["black_list"] = data["black_list"].split(",")
        data["white_list"] = data["white_list"].split(",")

        if data["black_list"] == [""]:
            data["black_list"] = []
        if data["white_list"] == [""]:
            data["white_list"] = []
        sv.set(False, **data)


@app.post("/genshinuid/setPlugins/{name}")
@site.auth.requires("root")
def _set_Plugins(request: Request, data: Dict, name: str):
    if name in SL.plugins:
        plugin = SL.plugins[name]
        data["pm"] = int(data["pm"])
        data["priority"] = int(data["priority"])
        if "prefix" in data:
            data["prefix"] = data["prefix"].split(",")

        data["black_list"] = data["black_list"].split(",")
        data["white_list"] = data["white_list"].split(",")

        if data["black_list"] == [""]:
            data["black_list"] = []
        if data["white_list"] == [""]:
            data["white_list"] = []
        plugin.set(False, **data)


@app.post("/genshinuid/setGsConfig/{config_name}")
@site.auth.requires("root")
def _set_Config(request: Request, data: Dict, config_name: str):
    for name in data:
        if name == "params":
            continue
        cc = all_config_list[config_name]
        config = cc[name]
        if isinstance(config, GsListStrConfig):
            data[name] = data[name].replace("：", ":").replace(":", ",")
            value = data[name].split(",")
        elif isinstance(config, GsImageConfig):
            value: dict = cc.config_default[name].data  # type: ignore
        else:
            value = data[name]
        cc.set_config(name, value)
    return {"status": 0, "msg": "成功！"}


@app.post("/genshinuid/setCoreConfig")
@site.auth.requires("root")
def _set_Core_Config(request: Request, data: Dict):
    result = {}
    for i in data:
        if (i in CONFIG_DEFAULT and isinstance(CONFIG_DEFAULT[i], List)) or i in ["log_output"]:
            v = data[i].split(",")
        else:
            v = data[i]

        if i in ["log_level", "log_output"]:
            g = i.split("_")
            k = g[0]
            if k not in result:
                result[k] = {}
            result[k][g[1]] = v
            continue

        core_config.set_config(i, v)

    for r in result:
        core_config.set_config(r, result[r])

    return {"status": 0, "msg": "成功！"}


@app.get("/genshinuid/api/getPlugins")
@site.auth.requires("root")
async def _get_plugins(request: Request):
    tasks = []
    plugins_list = await get_plugins_list()
    for name in plugins_list:
        plugin = plugins_list[name]
        link = plugin["link"]
        plugin_name = link.split("/")[-1]
        sample = {
            "label": plugin_name,
            "key": name,
            "status": await check_status(plugin_name),
            "remark": plugin["info"],
        }
        tasks.append(sample)

    return tasks


@app.get("/genshinuid/api/getAnalysisData/{bot_id}/{bot_self_id}")
@site.auth.requires("root")
async def _get_data_analysis(
    request: Request,
    bot_id: Optional[str],
    bot_self_id: Optional[str],
):
    if bot_id == "None" or bot_self_id == "None":
        bot_id = None
        bot_self_id = None

    xaxis = []
    series = []

    send_data = []
    receive_data = []
    command_data = []
    image_gen_data = []

    datas = await CoreDataSummary.get_day_trends(bot_id, bot_self_id)

    if bot_id is None or bot_self_id is None:
        key = "all_bots"
    else:
        key = "bot"

    for day in range(46):
        daystr = (datetime.now() - timedelta(days=45) + timedelta(days=day)).strftime("%m-%d")
        xaxis.append(daystr)
        send_data.append(datas[f"{key}_send"][day])
        receive_data.append(datas[f"{key}_receive"][day])
        command_data.append(datas[f"{key}_command"][day])
        image_gen_data.append(datas[f"{key}_image"][day])

    series.append({"name": "发送", "type": "line", "data": send_data})
    series.append(
        {
            "name": "接收",
            "type": "line",
            "data": receive_data,
        }
    )
    series.append(
        {
            "name": "命令调用",
            "type": "line",
            "data": command_data,
        }
    )
    series.append(
        {
            "name": "图片生成",
            "type": "line",
            "data": image_gen_data,
        }
    )

    data = {
        "title": {"text": ""},
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["发送", "接收", "命令调用", "图片生成"]},
        "xAxis": {"type": "category", "boundaryGap": False, "data": xaxis},
        "yAxis": {"type": "value"},
        "series": series,
    }

    return {"status": 0, "msg": "数据获取成功！", "data": data}


@app.get("/genshinuid/api/getAnalysisUserGroup/{bot_id}/{bot_self_id}")
@site.auth.requires("root")
async def _get_usergroup_analysis(
    request: Request,
    bot_id: Optional[str],
    bot_self_id: Optional[str],
):
    if bot_id == "None" or bot_self_id == "None":
        bot_id = None
        bot_self_id = None
    xaxis = []
    series = []

    group_data = []
    user_data = []

    datas = await CoreDataSummary.get_day_trends(bot_id, bot_self_id)

    if bot_id is None or bot_self_id is None:
        group_key = "all_bots_group_count"
        user_key = "all_bots_user_count"
    else:
        group_key = "bot_group_count"
        user_key = "bot_user_count"

    for day in range(46):
        daystr = (datetime.now() - timedelta(days=45) + timedelta(days=day)).strftime("%m-%d")
        xaxis.append(daystr)
        group_data.append(datas[group_key][day])
        user_data.append(datas[user_key][day])

    series.append({"name": "用户", "type": "bar", "data": user_data})
    series.append(
        {
            "name": "群组",
            "type": "bar",
            "data": group_data,
        }
    )

    data = {
        "title": {"text": ""},
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["用户", "群组"]},
        "xAxis": {"type": "category", "boundaryGap": False, "data": xaxis},
        "yAxis": {"type": "value"},
        "series": series,
    }

    return {"status": 0, "msg": "数据获取成功！", "data": data}


@app.post("/genshinuid/api/updatePlugins")
@site.auth.requires("root")
async def _update_plugins(request: Request, data: Dict):
    repo = check_plugins(data["label"])
    if repo:
        if check_can_update(repo):
            try:
                await update_plugins(data["label"])
                retcode = 0
            except:  # noqa:E722
                retcode = -1
        else:
            retcode = 0
    else:
        try:
            retcode = await install_plugin(data["key"])
            retcode = 0
        except:  # noqa:E722
            retcode = -1
    return {"status": retcode, "msg": "", "data": {}}


@app.post("/genshinuid/api/BatchPush")
@site.auth.requires("root")
async def _batch_push(request: Request, data: Dict):
    send_msg = data["push_text"]
    soup = BeautifulSoup(send_msg, "lxml")

    msg: List[Message] = []
    text_list: List[Tag] = list(soup.find_all("p"))  # type: ignore
    for text in text_list:
        msg.append(MessageSegment.text(str(text)[3:-4] + "\n"))

    img_tag: List[Tag] = list(soup.find_all("img"))  # type: ignore
    for img in img_tag:
        src: str = img.get("src")  # type: ignore
        width: str = img.get("width")  # type: ignore
        height: str = img.get("height")  # type: ignore

        base64_data = "base64://" + src.split(",")[-1]

        msg.append(MessageSegment.image(base64_data))
        msg.append(MessageSegment.image_size((int(width), int(height))))

    send_target: List[str] = data["push_tag"].split(",")
    push_bots: List[str] = data["push_bot"].split(",")
    user_sends: Dict[str, List[str]] = {}
    group_sends: Dict[str, List[str]] = {}

    if "ALLUSER" in send_target:
        all_user = await CoreUser.get_all_user()
        if all_user:
            for user in all_user:
                if user.bot_id not in user_sends:
                    user_sends[user.bot_id] = [user.user_id]
                else:
                    if user.user_id not in user_sends[user.bot_id]:
                        user_sends[user.bot_id].append(user.user_id)
        send_target.remove("ALLUSER")

    if "ALLGROUP" in send_target:
        all_group = await CoreGroup.get_all_group()
        if all_group:
            for group in all_group:
                if group.bot_id not in group_sends:
                    group_sends[group.bot_id] = [group.group_id]
                else:
                    if group.group_id not in group_sends[group.bot_id]:
                        group_sends[group.bot_id].append(group.group_id)
        send_target.remove("ALLGROUP")

    for _target in send_target:
        if "|" not in _target:
            continue
        targets = _target.split("|")
        target, bot_id = targets[0], targets[1]
        if target.startswith("g:"):
            group_id = target.split(":")[1]
            if bot_id not in group_sends:
                group_sends[bot_id] = [group_id]
            else:
                if group_id not in group_sends[bot_id]:
                    group_sends[bot_id].append(group_id)
        else:
            user_id = target.split(":")[1]
            if bot_id not in user_sends:
                user_sends[bot_id] = [user_id]
            else:
                if user_id not in user_sends[bot_id]:
                    user_sends[bot_id].append(user_id)

    s = [group_sends, user_sends]
    for BOT_ID in gss.active_bot:
        if BOT_ID not in push_bots:
            continue
        for index, sends in enumerate(s):
            send_type = "group" if index == 0 else "direct"
            for bot_id in sends:
                for uuid in sends[bot_id]:
                    if index == 0:
                        msg.append(Message("group", uuid))
                    await gss.active_bot[BOT_ID].target_send(
                        msg,
                        send_type,
                        uuid,
                        bot_id,
                        "",
                        "",
                    )

    return {"status": 0, "msg": "推送成功！", "data": "推送成功！"}


async def delete_image(image_path: Path):
    await asyncio.sleep(int(pic_expire_time))
    image_path.unlink()


app.mount(
    "/webstatic",
    StaticFiles(directory=Path(__file__).parent / "webstatic"),
    name="static",
)


@app.head("/genshinuid/image/{image_id}")
@app.get("/genshinuid/image/{image_id}")
async def get_image(image_id: str, background_tasks: BackgroundTasks):
    path = image_res / image_id
    if not path.exists() and "." not in image_id:
        path = image_res / f"{image_id}.jpg"

    if not path.exists():
        return Response(status_code=404)

    image = Image.open(path).convert("RGB")
    image_bytes = BytesIO()
    image.save(image_bytes, format="JPEG")
    image_bytes.seek(0)
    response = StreamingResponse(image_bytes, media_type="image/png")
    if is_clean_pic:
        asyncio.create_task(delete_image(path))
    return response


@app.get("/corelogs")
@site.auth.requires("root")
async def core_log(request: Request):
    return StreamingResponse(read_log(), media_type="text/event-stream")


@app.post("/genshinuid/api/loadData{count}/{bot_id}/{bot_self_id}")
@site.auth.requires("root")
async def get_history_data(
    request: Request,
    data: Dict,
    count: int,
    bot_id: Optional[str],
    bot_self_id: Optional[str],
):
    name = data.get("name", None)
    if name is None:
        date = dt_date.today()
    else:
        date = dt_date.fromisoformat(name)

    date_format = date.strftime("%Y-%m-%d")

    if count == 1:
        if bot_id == "None" or bot_self_id == "None":
            bot_id = None
            bot_self_id = None

        datas: Sequence[CoreDataAnalysis] = await CoreDataAnalysis.get_sp_data(
            date,
            bot_id,
            bot_self_id,
        )

        c_data: Dict[str, int] = {}
        g_data: Dict[str, Dict[str, int]] = {}
        u_data: Dict[str, Dict[str, int]] = {}
        for d in datas:
            if d.data_type == DataType.USER:
                if d.command_name not in c_data:
                    c_data[d.command_name] = 0
                c_data[d.command_name] += d.command_count

                if d.target_id not in u_data:
                    u_data[d.target_id] = {}
                if d.command_name not in u_data[d.target_id]:
                    u_data[d.target_id][d.command_name] = 0
                u_data[d.target_id][d.command_name] += d.command_count

            if d.data_type == DataType.GROUP:
                if d.target_id not in g_data:
                    g_data[d.target_id] = {}
                if d.command_name not in g_data[d.target_id]:
                    g_data[d.target_id][d.command_name] = 0
                g_data[d.target_id][d.command_name] += d.command_count

        TEMP_DICT[f"{bot_id}/{bot_self_id}/{date_format}"] = {
            "c_data": c_data,
            "g_data": g_data,
            "u_data": u_data,
        }
    else:
        while f"{bot_id}/{bot_self_id}/{date_format}" not in TEMP_DICT:
            await asyncio.sleep(1)
        c_data = TEMP_DICT[f"{bot_id}/{bot_self_id}/{date_format}"]["c_data"]
        g_data = TEMP_DICT[f"{bot_id}/{bot_self_id}/{date_format}"]["g_data"]
        u_data = TEMP_DICT[f"{bot_id}/{bot_self_id}/{date_format}"]["u_data"]

    # 对c_data按值从大到小排序
    sorted_items = sorted(c_data.items(), key=lambda x: x[1], reverse=True)
    y_data = [k for k, v in sorted_items]
    series_data = [v for k, v in sorted_items]

    if count == 1:
        echarts_option = {
            "title": {"text": "命令使用量"},
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
            "legend": {},
            "grid": {
                "left": "3%",
                "right": "4%",
                "bottom": "3%",
                "containLabel": True,
            },
            "xAxis": {"type": "value", "boundaryGap": [0, 0.01]},
            "yAxis": {
                "type": "category",
                "data": y_data,
            },
            "series": [
                {
                    "name": "命令",
                    "type": "bar",
                    "data": series_data,
                }
            ],
        }
    elif count == 2:
        # 对g_data进行筛选，仅保留命令数量总和前20的target_id，并按数量降序排序
        # 计算每个target_id的命令总数
        group_total = {gid: sum(cmds.values()) for gid, cmds in g_data.items()}
        # 取前20个target_id
        top_groups = sorted(group_total.items(), key=lambda x: x[1], reverse=True)[:20]
        # 构建新的g_data，只保留前20
        g_data = {gid: g_data[gid] for gid, _ in top_groups}

        uall_series: List[str] = y_data[:8] + ["其他命令"]
        u_series = []
        ufinnal_count: Dict[str, List[int]] = {c: [] for c in uall_series}
        for g in g_data:
            others = 0
            group_cmds = g_data[g]
            for cmd in group_cmds:
                if cmd in ufinnal_count:
                    ufinnal_count[cmd].append(g_data[g][cmd])
                else:
                    others += g_data[g][cmd]

            for cmd in y_data[:8]:
                if cmd not in group_cmds:
                    ufinnal_count[cmd].append(0)

            ufinnal_count["其他命令"].append(others)

        for f in ufinnal_count:
            u_series.append(
                {
                    "name": f,
                    "type": "bar",
                    "stack": "total",
                    "label": {"show": True},
                    "emphasis": {"focus": "series"},
                    "data": ufinnal_count[f],
                }
            )

        echarts_option = {
            "title": {"text": "群组命令触发量"},
            "tooltip": {
                "trigger": "axis",
                "axisPointer": {"type": "shadow"},
            },
            "legend": {},
            "grid": {
                "left": "3%",
                "right": "4%",
                "bottom": "3%",
                "containLabel": True,
            },
            "xAxis": {"type": "value"},
            "yAxis": {
                "type": "category",
                "data": list(g_data.keys()),
            },
            "series": u_series,
        }

    else:
        user_total = {gid: sum(cmds.values()) for gid, cmds in u_data.items()}
        top_users = sorted(user_total.items(), key=lambda x: x[1], reverse=True)[:20]
        u_data = {gid: u_data[gid] for gid, _ in top_users}

        all_series: List[str] = y_data[:8] + ["其他命令"]
        u_series = []
        finnal_count: Dict[str, List[int]] = {c: [] for c in all_series}
        for gid in u_data:
            others = 0
            user_cmds = u_data[gid]
            for cmd in user_cmds:
                if cmd in finnal_count:
                    finnal_count[cmd].append(user_cmds[cmd])
                else:
                    others += user_cmds[cmd]

            for cmd in y_data[:8]:
                if cmd not in user_cmds:
                    finnal_count[cmd].append(0)

            finnal_count["其他命令"].append(others)

        for f in finnal_count:
            u_series.append(
                {
                    "name": f,
                    "type": "bar",
                    "stack": "total",
                    "label": {"show": True},
                    "emphasis": {"focus": "series"},
                    "data": finnal_count[f],
                }
            )

        echarts_option = {
            "title": {"text": "个人命令触发量"},
            "tooltip": {
                "trigger": "axis",
                "axisPointer": {"type": "shadow"},
            },
            "legend": {},
            "grid": {
                "left": "3%",
                "right": "4%",
                "bottom": "3%",
                "containLabel": True,
            },
            "xAxis": {"type": "value"},
            "yAxis": {
                "type": "category",
                "data": list(u_data.keys()),
            },
            "series": u_series,
        }

    return {
        "status": 0,
        "msg": "ok",
        "data": echarts_option,
    }


@app.get("/genshinuid/api/historyLogs")
@site.auth.requires("root")
async def get_history_logs(
    request: Request,
    date: Optional[str] = None,
    page: int = 0,
    perPage: int = 0,
):
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    if date.endswith(".log"):
        date = date.removesuffix(".log")

    history_log_data = HistoryLogData()
    log_files = await history_log_data.get_parse_logs(LOG_PATH / f"{date}.log")
    total = len(log_files)
    if page != 0 and perPage != 0:
        start = (page - 1) * perPage
        end = start + perPage
        log_file = log_files[start:end]
    else:
        log_file = log_files
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "count": total,
            "rows": log_file,
        },
    }
