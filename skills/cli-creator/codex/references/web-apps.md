# Web app sources

Use this reference for commands derived from browser workflows or DevTools requests, together with [API clients](api-clients.md) when the CLI calls the app's endpoints.

Before implementing from captured requests, create sanitized endpoint notes for the relevant jobs. Record method and path, required headers, auth and CSRF behavior, request body, response ID fields, pagination, errors, and a redacted response example where available. Never commit copied cookies, bearer tokens, customer secrets, or full production payloads.

Use screenshots to identify workflow, UI vocabulary, fields, and confirmation points. Verify API behavior with a network request, export, documentation, or fixture; screenshots alone do not establish an API contract.
