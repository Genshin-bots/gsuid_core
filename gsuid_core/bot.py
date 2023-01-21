import websockets.server


class Bot(websockets.server.WebSocketServerProtocol):
    def __init__(self, _id: str):
        self.bot_id = _id
