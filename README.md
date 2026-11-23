# Paperclip exporter

[![trivy](https://github.com/visibilityspots/dockerfile-paperclip-exporter/actions/workflows/trivy.yml/badge.svg)](https://github.com/visibilityspots/dockerfile-paperclip-exporter/actions/workflows/trivy.yml)
[![docker-hub-description](https://github.com/visibilityspots/dockerfile-paperclip-exporter/actions/workflows/docker-hub-description.yml/badge.svg)](https://github.com/visibilityspots/dockerfile-paperclip-exporter/actions/workflows/docker-hub-description.yml)
[![build status](https://github.com/visibilityspots/dockerfile-paperclip-exporter/actions/workflows/main.yml/badge.svg)](https://github.com/visibilityspots/dockerfile-paperclip-exporter/actions/workflows/main.yml)
[![gitHub release](https://img.shields.io/github/v/release/visibilityspots/dockerfile-paperclip-exporter)](https://github.com/visibilityspots/dockerfile-paperclip-exporter/releases)
[![docker image size](https://img.shields.io/docker/image-size/visibilityspots/paperclip-exporter/latest)](https://hub.docker.com/r/visibilityspots/paperclip-exporter)
[![docker pulls](https://img.shields.io/docker/pulls/visibilityspots/paperclip-exporter.svg)](https://hub.docker.com/r/visibilityspots/paperclip-exporter/)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](https://opensource.org/licenses/MIT)

a Prometheus exporter for a self-hosted [Paperclip](https://github.com/paperclipai/paperclip) instance.

Paperclip has no metrics endpoint of its own. This exporter polls its REST API every minute
and serves what it finds on `/metrics`: whether the AI connection your agents use is still
connected, the state of every agent, failed heartbeat runs, the last run of each routine and
the number of issues per status. That is enough to alert on a lost Claude login, a routine
that stopped running or a pile of blocked issues before anyone notices by hand.

It is a single Python file without dependencies, running as a non-root user.

## run

```
$ docker run -d --name paperclip-exporter -p 9878:9878 \
    -e PAPERCLIP_API=https://paperclip.example.com/api \
    -e PAPERCLIP_COMPANY_ID=<company uuid> \
    -e PAPERCLIP_API_KEY=<board api key> \
    visibilityspots/paperclip-exporter:latest
$ curl -s localhost:9878/metrics | grep paperclip_up
paperclip_up 1
```

### API key

The exporter only reads. A user with the `viewer` role is enough, and also sees AI
connections that are shared with the company. Create a dedicated user, log in as that user
with the Paperclip CLI and create a board API key without expiry; board keys expire after
30 days by default:

```
$ curl -s -X POST -H "Authorization: Bearer <cli token>" -H 'Content-Type: application/json' \
    -d '{"name":"paperclip-exporter","expiresAt":null}' \
    https://paperclip.example.com/api/board-api-keys
```

Leave `requestedCompanyId` out of that request: with it, Paperclip runs a company check
that refuses a POST from a viewer (`Viewer access is read-only`). Revoke the CLI key
afterwards.

### Health

`/healthz` answers `200 ok` whenever the process runs, independent of Paperclip. An
unreachable API or a revoked key shows up as `paperclip_up 0`, not as a dead exporter.

## Configuration

| variable | default | |
|---|---|---|
| `PAPERCLIP_API` | | base url of the API, ending in `/api` (required) |
| `PAPERCLIP_COMPANY_ID` | | uuid of the company to export (required) |
| `PAPERCLIP_API_KEY` | | board API key (required) |
| `METRICS_PORT` | `9878` | listen port for `/metrics` and `/healthz` |
| `POLL_INTERVAL` | `60` | seconds between two polls |
| `HTTP_TIMEOUT` | `20` | timeout per API request in seconds |

When one of the required variables is empty (or the literal `<no value>` a consul-template
render leaves for a missing key), the exporter does not poll and reports `paperclip_up 0`.

### Metrics

| metric | labels | meaning |
|---|---|---|
| `paperclip_up` | | 1 when the last poll of every endpoint succeeded |
| `paperclip_last_poll_success_timestamp_seconds` | | last fully successful poll, 0 = never |
| `paperclip_poll_duration_seconds` | | duration of the last poll |
| `paperclip_poll_errors_total` | `endpoint` | failed API calls since start |
| `paperclip_info` | `version` | server version from `/api/health` |
| `paperclip_health_ok` | | 1 when `/api/health` reports `ok` |
| `paperclip_ai_connection_connected` | `connection`, `provider`, `status`, `in_use` | 1 when the connection is `connected`; `in_use` is true when an agent points to it |
| `paperclip_agent_info` | `agent`, `agent_id`, `status`, `lifecycle_state` | always 1 |
| `paperclip_agent_paused` | `agent`, `agent_id` | 1 when paused |
| `paperclip_agent_error` | `agent`, `agent_id` | 1 when the agent reports an error |
| `paperclip_agent_last_heartbeat_timestamp_seconds` | `agent`, `agent_id` | last heartbeat |
| `paperclip_runs` | `status` | heartbeat runs in the API response |
| `paperclip_runs_truncated` | | 1 when the run list hit the 1000 row limit |
| `paperclip_run_last_failure_timestamp_seconds` | `error_code` | finish time of the latest failed run per error code |
| `paperclip_run_last_finished_timestamp_seconds` | | finish time of the latest run |
| `paperclip_routines` | | number of routines |
| `paperclip_routine_active` | `routine`, `routine_id` | 1 when the routine is active |
| `paperclip_routine_last_run_timestamp_seconds` | `routine`, `routine_id` | last run of the routine |
| `paperclip_routine_last_run_success` | `routine`, `routine_id` | 1 when the last run reached `completed`; `issue_created` is not success |
| `paperclip_issues` | `status` | issues per status |

Labels never carry free text: names are lowercased and stripped to `[a-z0-9_-]`, template
parts like `{{date}}` are removed, ids are cut to 8 characters and error codes outside a
fixed list become `other`.

Failed runs are a timestamp, not a counter. The exporter keeps no state, so a counter would
count the whole run history again after every restart.

### Alerting

A starting point for Prometheus rules:

```yaml
- alert: PaperclipAiConnectionLost
  expr: paperclip_ai_connection_connected{in_use="true"} == 0 or absent(paperclip_ai_connection_connected{in_use="true"})
  for: 5m
- alert: PaperclipRunFailed
  expr: paperclip_run_last_failure_timestamp_seconds > time() - 3600
- alert: PaperclipRoutineStale
  expr: time() - paperclip_routine_last_run_timestamp_seconds > 26 * 3600
  for: 15m
- alert: PaperclipApiFailing
  expr: paperclip_up == 0 or (time() - paperclip_last_poll_success_timestamp_seconds > 600)
  for: 10m
```

Filter the connection alert on `in_use="true"`: a reconnect in Paperclip creates a new
connection and leaves the old one on `needs_attention` for good.

## build

```
$ docker build -t visibilityspots/paperclip-exporter:latest .
```

### buildx

```
$ docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
$ docker buildx build -t visibilityspots/paperclip-exporter:latest --platform linux/amd64,linux/arm/v6,linux/arm/v7,linux/arm64 --push .
```

### dgoss

The tests in goss.yaml run the real entrypoint without an API key, so the exporter starts,
serves `/metrics` and `/healthz` and refuses to poll. `goss_wait.yaml` waits until it
listens:

```
$ dgoss run -e PAPERCLIP_API=http://127.0.0.1:1/api -e PAPERCLIP_COMPANY_ID=test visibilityspots/paperclip-exporter:dev
INFO: Creating podman container
INFO: Copy goss files into container
INFO: Starting podman container
INFO: Container ID: afc079c3
INFO: Found goss_wait.yaml, waiting for it to pass before running tests
INFO: Sleeping for 0.2
INFO: Container health
INFO: Running Tests
File: /opt/paperclip-exporter/exporter.py: exists: matches expectation: true
User: exporter: exists: matches expectation: true
User: exporter: uid: matches expectation: 10001
Process: python3: running: matches expectation: true
Command: id -u: exit-status: matches expectation: 0
Command: id -u: stdout: matches expectation: ["10001"]
HTTP: http://127.0.0.1:9878/metrics: status: matches expectation: 200
HTTP: http://127.0.0.1:9878/metrics: Body: matches expectation: ["paperclip_up 0","paperclip_last_poll_success_timestamp_seconds 0"]
Command: python3 --version: exit-status: matches expectation: 0
HTTP: http://127.0.0.1:9878/healthz: status: matches expectation: 200
HTTP: http://127.0.0.1:9878/healthz: Body: matches expectation: ["ok"]
Port: tcp:9878: listening: matches expectation: true


Total Duration: 0.002s
Count: 12, Failed: 0, Skipped: 0
INFO: Deleting container
```

### act

using [act](https://github.com/nektos/act#overview----) for local testing of the written github actions makes my life and commit history a lot easier;

```
Stage  Job ID  Job name  Workflow name           Workflow file               Events
0      update  update    docker-hub-description  docker-hub-description.yml  push
0      main    main      CI                      main.yml                    push,pull_request
0      scan    scan      trivy                   trivy.yml                   workflow_dispatch,push,schedule
```

### release

Images are built and pushed by the shared workflow in
[visibilityspots/github-workflows](https://github.com/visibilityspots/github-workflows) when
a tag is pushed. Pushing to `main` builds nothing; a pull request only builds and runs dgoss.

## License

Distributed under the [MIT license](https://github.com/visibilityspots/dockerfile-paperclip-exporter/blob/main/LICENSE)
