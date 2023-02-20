from server import GsServer

gss = GsServer()
if not gss.is_load:
    gss.is_load = True
    gss.load_plugins()
