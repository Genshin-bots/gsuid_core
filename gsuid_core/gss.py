from gsuid_core.aps import scheduler
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
        scheduler.remove_job(i.id)

del repeat_jobs
