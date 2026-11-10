# docat

A simple, zero-dependency, read-only CLI wrapper for the Docker Engine API over Unix or TCP sockets.

## Features

- **Strictly read-only**: Only performs `GET` requests against the Docker socket.
- **Zero external dependencies**: Uses only the Python standard library.
- **Unix & TCP sockets**: Connect to `unix:///var/run/docker.sock`, `/var/run/docker.sock`, `tcp://host:2375`, `http://host:2375`, or `https://host:2376`.
- **Flexible output**: Output as aligned tables (`table`), pretty/compact JSON (`json`), or YAML (`yaml`).
- **Environment & CLI flags**: Configure defaults via environment variables (`DOCAT_HOST` / `DOCKER_HOST`, `DOCAT_OUTPUT`, `DOCAT_ALL`, etc.) or CLI flags.

## Usage

Run directly with `uv` or `python3`:

```sh
uv run docat <resource> [action] [target] [flags]
# or
python3 main.py <resource> [action] [target] [flags]
```

### Docker

```sh
docker build -t docat .

# Connect via mounted Unix socket
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock:ro docat containers

# Connect via TCP socket
docker run --rm docat -H tcp://host.docker.internal:2375 containers -o yaml
```

### Supported Resources & Commands

| Resource | Aliases | Actions | Description |
| :--- | :--- | :--- | :--- |
| `containers` | `container`, `c`, `ps` | `list` (default), `inspect <id>`, `logs <id>`, `top <id>`, `stats <id>`, `changes <id>` | Containers |
| `images` | `image`, `img`, `i` | `list` (default), `inspect <id>`, `history <id>` | Images |
| `networks` | `network`, `net`, `n` | `list` (default), `inspect <id>` | Networks |
| `volumes` | `volume`, `vol`, `v` | `list` (default), `inspect <name>` | Volumes |
| `swarm` | | `inspect` (default) | Swarm cluster info |
| `nodes` | `node` | `list` (default), `inspect <id>` | Swarm nodes |
| `services` | `service`, `svc` | `list` (default), `inspect <id>`, `logs <id>` | Swarm services |
| `tasks` | `task` | `list` (default), `inspect <id>` | Swarm tasks |
| `configs` | `config` | `list` (default), `inspect <id>` | Swarm configs |
| `secrets` | `secret` | `list` (default), `inspect <id>` | Swarm secrets |
| `plugins` | `plugin` | `list` (default), `inspect <name>` | Docker plugins |
| `info` | `system info` | | System-wide information |
| `version` | `system version` | | Docker version information |
| `ping` | `system ping` | | Ping the Docker server |
| `df` | `system df` | | Disk usage summary |
| `events` | `system events` | | Past events (`--since`, `--until`) |
| `inspect <id>`| | | Auto-detect and inspect any resource by ID/name |
| `logs <id>` | | | Fetch container logs |
| `raw <path>` | `get <path>` | | Perform a raw `GET` on any Docker API endpoint |

### Examples

```sh
# List running or all containers
docat containers
docat ps -a
docat containers -a -o json
docat containers -o yaml

# Filter results (repeatable -f key=value)
docat containers -f status=running -f label=app=web
docat tasks -f desired-state=running

# Inspect any resource (shorthand `docat <resource> <id>` also works)
docat container inspect my-container
docat container my-container -o yaml
docat image inspect nginx:alpine -o yaml
docat inspect my-container

# Connect to Unix or TCP sockets
docat -H unix:///var/run/docker.sock version
docat -H tcp://127.0.0.1:2375 images
docat -H 10.0.0.10:2375 nodes -o yaml

# Container logs, processes, stats
docat container logs my-container --tail 50 -t
docat container top my-container
docat container stats my-container -o yaml
```

### Environment Variables

| Variable | Fallback | Description |
| :--- | :--- | :--- |
| `DOCAT_HOST` | `DOCKER_HOST` | Socket address (`unix:///var/run/docker.sock` by default) |
| `DOCAT_OUTPUT` | `DOCAT_FORMAT` | Default output format (`auto`, `table`, `json`, `yaml`) |
| `DOCAT_COMPACT` | | `1` / `true` for compact single-line JSON |
| `DOCAT_ALL` | | `1` / `true` to include all containers/images by default |
| `DOCAT_TAIL` | | Default number of log lines (default: `100`) |
| `DOCAT_TIMEOUT` | | Socket timeout in seconds (default: `10.0`) |
| `DOCAT_API_VERSION` | `DOCKER_API_VERSION` | Optional API version prefix (e.g. `1.45`) |
| `DOCAT_CERT_PATH` | `DOCKER_CERT_PATH` | Directory containing `ca.pem`, `cert.pem`, `key.pem` |
| `DOCAT_TLS_VERIFY` | `DOCKER_TLS_VERIFY` | Verify TLS certificates (`true` by default) |
