# Media and artifact flows

Use this reference for media, artifacts, or presigned uploads. For service calls, also read [API clients](api-clients.md).

Model the service's actual phases, such as creating an upload, transferring bytes, polling processing status, and attaching the resulting ID. Verify each implemented phase, including failure and incomplete-transfer handling where relevant. Use fixtures or authorized draft operations for phases that would otherwise mutate a live service.

Return the stable ID or file path needed by the next phase. Keep signed URLs and auth headers out of logs and committed fixtures.
