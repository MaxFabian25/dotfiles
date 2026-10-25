# Configuration

| Setting | Default | Description |
| --- | --- | --- |
| `timezone` | `"UTC"` | The default timezone for jobs that don't set their own timezone. |
| `max_workers` | `4` | The number of jobs that can run at the same time. |
| `misfire_grace` | `60` | The number of seconds a job can start late before cronlet skips the run. |

cronlet reads timezone data from the operating system through `zoneinfo`. On systems without
timezone data, install the `tzdata` package.
