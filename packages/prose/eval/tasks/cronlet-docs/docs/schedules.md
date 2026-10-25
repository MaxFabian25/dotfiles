# Schedules

A schedule is a cron expression plus a timezone. cronlet reads the expression in the schedule's
timezone, not in the timezone of the server.

## Set the timezone for a job

Pass an IANA timezone name to `@scheduler.job`:

```python
@scheduler.job("30 2 * * *", timezone="America/New_York")
def rotate_logs():
    ...
```

If a job has no timezone, it uses the scheduler's default timezone. If the scheduler has no
default timezone, cronlet uses UTC.

## Supported expressions

cronlet supports the standard five-field cron syntax, ranges, steps, and the `MON`-`SUN` and
`JAN`-`DEC` names. It doesn't support the seconds field or the `L` and `W` modifiers.
