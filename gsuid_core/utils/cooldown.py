import time


class CooldownTracker:
    def __init__(self):
        self.timestamps = {}

    def is_on_cooldown(self, user_id: str, cooldown: float):
        now = time.time()
        last_time = self.timestamps.get(user_id, 0)
        print(self.timestamps)
        if now - last_time >= cooldown:
            self.timestamps[user_id] = now
            return False
        return True


cooldown_tracker = CooldownTracker()
