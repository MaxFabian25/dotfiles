from cronlet.schedule import Schedule


class Scheduler:
    def __init__(self, timezone: str = "UTC", max_workers: int = 4, misfire_grace: int = 60):
        self.timezone = timezone
        self.max_workers = max_workers
        self.misfire_grace = misfire_grace
        self._jobs = []

    def job(self, expression: str, timezone: str | None = None):
        def register(func):
            self._jobs.append((func, Schedule(expression, timezone or self.timezone)))
            return func
        return register
