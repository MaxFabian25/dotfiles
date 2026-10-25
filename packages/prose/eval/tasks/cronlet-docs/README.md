# cronlet

cronlet runs Python functions on cron schedules inside your own process. Each schedule has a
timezone, so a job set for 09:00 runs at 09:00 local time in that timezone.

```python
from cronlet import Scheduler

scheduler = Scheduler(timezone="Europe/Copenhagen")

@scheduler.job("0 9 * * MON-FRI")
def send_report():
    ...
```

## Documentation

- [Schedules](docs/schedules.md)
- [Configuration](docs/configuration.md)
