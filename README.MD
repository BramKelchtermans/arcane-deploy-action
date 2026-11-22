# Deploy to Arcane - GitHub/Gitea Action

## Description

This action deploys multiple Docker Compose files to an [Arcane](https://getarcane.app)
manager (v3.x). It detects changes in Docker Compose files and either updates the
existing project or creates a new one in the matching Arcane environment (agent).

It is a drop-in successor to
[`portainer-deploy-action`](https://github.com/BramKelchtermans/portainer-deploy-action):
same repository layout, same `changed_files_path` / `environment_file` inputs, but
it talks to Arcane instead of Portainer.

## Repo structure

The first path segment is the Arcane environment name (= the agent name, or
`arcane` for the manager itself). The second segment is the stack name, which
becomes the Arcane project name and therefore the compose project name.

```
.
├── .gitea/workflows
│   └── deploy_arcane.yml
├── dockers
│   ├── service1
│   │   └── docker-compose.yml
│   └── service2
│       └── docker-compose.yml
└── work-dockers
    └── serviceN
        └── docker-compose.yml
```

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `arcane_url` | yes | — | Manager URL, e.g. `http://10.2.0.119:3552` |
| `arcane_api_key` | yes | — | Arcane API key, sent as `X-Api-Key` |
| `changed_files_path` | yes | — | File containing the changed compose paths |
| `environment_file` | no | `default` | `.env` file to forward to the stacks |
| `deploy_action` | no | `up` | `up` (compose up) or `redeploy` (pull + down + up) |
| `pull_policy` | no | `if_not_present` | `if_not_present`, `always` or `never` |

`pull_policy: if_not_present` mirrors Portainer's "update stack": the compose is
re-applied with the images already on the host. Use `redeploy` + `always` when you
explicitly want new images.

## Example workflow

```yaml
name: Deploy to Arcane

on:
  push:
    paths:
      - '**/docker-compose.yml'

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Get changed files
        run: |
          git diff --name-only HEAD^ HEAD > changed_files.txt
          cat changed_files.txt

      - name: Deploy to Arcane
        uses: BramKelchtermans/arcane-deploy-action@main
        with:
          arcane_url: ${{ secrets.ARCANE_URL }}
          arcane_api_key: ${{ secrets.ARCANE_API_KEY }}
          changed_files_path: 'changed_files.txt'
          environment_file: '.env'
```

## How it talks to Arcane

Per changed `docker-compose.yml`:

1. `GET /api/agents` → resolve the environment by **name** (the first path segment).
2. `GET /api/agents/{id}/projects?name={stack}` → does the project exist?
3. New project → `POST /api/agents/{id}/projects` (`multipart/form-data` with a
   `project` part `{name, composeContent, envContent}` and a `manifest` part
   `{"fileChanges":[]}`).
   Existing project → `PATCH /api/agents/{id}/projects/{projectId}`
   `{composeContent, envContent}`.
4. `POST /api/agents/{id}/projects/{projectId}/deploy?action=up|redeploy`
   and follow the newline-delimited JSON stream until `{"done":true}`.

Only the variables actually referenced as `${NAME}` in the compose file are
forwarded from `environment_file`, exactly like the Portainer action.

## Notes

- Stack removals are **not** automatic: if a `docker-compose.yml` disappears from
  the change set, the action logs a warning and leaves the running project alone.
  Delete the Arcane project from the UI/API when the stack must really go.
- Arcane writes the stack to `<projects directory>/<stack>/compose.yaml` and
  `.env` on the host, owned by uid 65532 — the projects directory must be
  writable by that uid.
- A stack must be managed by exactly one manager. Disable the Portainer workflow
  for a stack before Arcane starts deploying it, otherwise both recreate the
  containers from different working directories.
- TLS verification is disabled for the manager connection (self-signed homelab
  certificates); put Arcane behind a proper certificate before exposing it.

## Author

Bram Kelchtermans
