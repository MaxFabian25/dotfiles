"""Compute the next run time of a cron expression in a given timezone."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cronlet.expression import CronExpression


class Schedule:
    def __init__(self, expression: str, timezone: str = "UTC"):
        self.expression = CronExpression.parse(expression)
        self.timezone = ZoneInfo(timezone)

    def next_run(self, after: datetime) -> datetime:
        """Return the next aware datetime after `after` at which the job runs.

        Wall-clock times that don't exist (the gap when clocks go forward) run at the
        first valid minute after the gap. For example, 02:30 on the spring-forward night
        in America/New_York runs at 03:00.

        Wall-clock times that happen twice (the overlap when clocks go back) run once, at
        the first occurrence (fold=0). Jobs with a minute or hour step, such as
        "*/15 * * * *", run on every real occurrence, so they run twice during the
        repeated hour.
        """
        local = after.astimezone(self.timezone)
        candidate = self.expression.next_wall_time(local.replace(tzinfo=None))
        if self._is_gap(candidate):
            return self._first_valid_after_gap(candidate)
        aware = candidate.replace(tzinfo=self.timezone, fold=0)
        if self.expression.has_step and self._is_overlap(candidate):
            return self._each_occurrence(candidate, after)
        return aware

    def _is_gap(self, wall: datetime) -> bool:
        aware = wall.replace(tzinfo=self.timezone)
        return aware.astimezone(ZoneInfo("UTC")).astimezone(self.timezone).replace(tzinfo=None) != wall

    def _is_overlap(self, wall: datetime) -> bool:
        a = wall.replace(tzinfo=self.timezone, fold=0).utcoffset()
        b = wall.replace(tzinfo=self.timezone, fold=1).utcoffset()
        return a != b

    def _first_valid_after_gap(self, wall: datetime) -> datetime:
        probe = wall
        while self._is_gap(probe):
            probe += timedelta(minutes=1)
        return probe.replace(tzinfo=self.timezone)

    def _each_occurrence(self, wall: datetime, after: datetime) -> datetime:
        first = wall.replace(tzinfo=self.timezone, fold=0)
        return first if first > after else wall.replace(tzinfo=self.timezone, fold=1)
