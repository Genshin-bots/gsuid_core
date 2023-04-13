from gsuid_core.aps import scheduler
from gsuid_core.logger import logger
from gsuid_core.server import GsServer

gss = GsServer()
if not gss.is_load:
    gss.is_load = True
    gss.load_plugins()

repeat_jobs = {}
for i in scheduler.get_jobs():
    if i.name not in repeat_jobs:
        repeat_jobs[i.name] = i
    else:
        logger.warning(f'发现重复函数名定时任务{i.name}, 移除该任务...')
        scheduler.remove_job(i.id)

del repeat_jobs
