# Python CLI setup

Use this reference after choosing Python. Check the installed interpreter and relevant repo conventions before scaffolding. If required tooling is missing, use another suitable installed runtime or obtain authorization to install it.

Prefer the standard library unless the workflow needs more:

- `argparse` for arguments and help, or `typer` when it simplifies a larger command hierarchy.
- `urllib.request` and `urllib.parse`, or an existing `requests` or `httpx` dependency, for HTTP.
- `json`, `csv`, `sqlite3`, `pathlib`, and `subprocess` for local files, data, and existing scripts.
- A `pyproject.toml` console script or executable wrapper for the installed command.
- `uv` or a virtual environment when dependencies require it.

Provide a documented install command that puts the tool on PATH. State whether it depends on `uv`, a virtual environment, or system Python. Ensure its imports and packaged resources resolve outside the source directory.
