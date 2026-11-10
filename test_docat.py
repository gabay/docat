import http.server
import json
import os
import socketserver
import struct
import tempfile
import threading
import unittest
import urllib.parse

import main as docat


MOCK_ROUTES = {
    "/_ping": (200, "text/plain", b"OK"),
    "/version": (200, "application/json", json.dumps({"Version": "27.3.1", "ApiVersion": "1.47"}).encode()),
    "/info": (200, "application/json", json.dumps({"ID": "ABCD:1234", "Containers": 2, "Images": 5}).encode()),
    "/system/df": (
        200,
        "application/json",
        json.dumps(
            {
                "Images": [{"Size": 100000000}],
                "Containers": [{"SizeRw": 2048}],
                "Volumes": [{"UsageData": {"Size": 4096}}],
                "BuildCache": [],
            }
        ).encode(),
    ),
    "/containers/json": (
        200,
        "application/json",
        json.dumps(
            [
                {
                    "Id": "a1b2c3d4e5f6000011112222",
                    "Names": ["/web-api"],
                    "Image": "nginx:alpine",
                    "State": "running",
                    "Status": "Up 2 hours",
                    "Ports": [{"IP": "0.0.0.0", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"}],
                }
            ]
        ).encode(),
    ),
    "/containers/web-api/json": (
        200,
        "application/json",
        json.dumps(
            {
                "Id": "a1b2c3d4e5f6000011112222",
                "Name": "/web-api",
                "State": {"Status": "running", "Running": True},
                "Config": {"Image": "nginx:alpine", "Env": ["PORT=8080", "DEBUG=false"]},
            }
        ).encode(),
    ),
    "/containers/web-api/top": (
        200,
        "application/json",
        json.dumps(
            {
                "Titles": ["PID", "USER", "TIME", "COMMAND"],
                "Processes": [["101", "root", "0:00", "nginx: master process"]],
            }
        ).encode(),
    ),
    "/containers/web-api/stats": (
        200,
        "application/json",
        json.dumps({"id": "a1b2c3d4e5f6", "memory_stats": {"usage": 1048576}}).encode(),
    ),
    "/containers/web-api/changes": (
        200,
        "application/json",
        json.dumps([{"Path": "/etc/nginx/conf.d", "Kind": 0}, {"Path": "/tmp/app.pid", "Kind": 1}]).encode(),
    ),
    "/images/json": (
        200,
        "application/json",
        json.dumps(
            [
                {
                    "Id": "sha256:99887766554433221100",
                    "RepoTags": ["nginx:alpine"],
                    "Created": 1700000000,
                    "Size": 42500000,
                }
            ]
        ).encode(),
    ),
    "/images/nginx%3Aalpine/json": (
        200,
        "application/json",
        json.dumps({"Id": "sha256:99887766554433221100", "RepoTags": ["nginx:alpine"]}).encode(),
    ),
    "/images/nginx%3Aalpine/history": (
        200,
        "application/json",
        json.dumps([{"Id": "sha256:998877665544", "Created": 1700000000, "Size": 1024, "CreatedBy": "/bin/sh -c echo hi"}]).encode(),
    ),
    "/networks": (
        200,
        "application/json",
        json.dumps([{"Id": "net1234567890abcdef", "Name": "bridge", "Driver": "bridge", "Scope": "local", "Internal": False}]).encode(),
    ),
    "/networks/bridge": (
        200,
        "application/json",
        json.dumps({"Id": "net1234567890abcdef", "Name": "bridge", "Driver": "bridge"}).encode(),
    ),
    "/volumes": (
        200,
        "application/json",
        json.dumps(
            {
                "Volumes": [
                    {
                        "Name": "db-data",
                        "Driver": "local",
                        "Scope": "local",
                        "Mountpoint": "/var/lib/docker/volumes/db-data/_data",
                    }
                ]
            }
        ).encode(),
    ),
    "/volumes/db-data": (
        200,
        "application/json",
        json.dumps({"Name": "db-data", "Driver": "local"}).encode(),
    ),
    "/swarm": (
        200,
        "application/json",
        json.dumps({"ID": "swarm1234567890", "Spec": {"Name": "default"}}).encode(),
    ),
    "/nodes": (
        200,
        "application/json",
        json.dumps(
            [
                {
                    "ID": "node1234567890abcdef",
                    "Spec": {"Role": "manager", "Availability": "active"},
                    "Description": {"Hostname": "mgr-01", "Engine": {"EngineVersion": "27.3.1"}},
                    "Status": {"State": "ready"},
                    "ManagerStatus": {"Leader": True},
                }
            ]
        ).encode(),
    ),
    "/nodes/mgr-01": (
        200,
        "application/json",
        json.dumps({"ID": "node1234567890abcdef", "Description": {"Hostname": "mgr-01"}}).encode(),
    ),
    "/services": (
        200,
        "application/json",
        json.dumps(
            [
                {
                    "ID": "svc1234567890abcdef",
                    "Spec": {
                        "Name": "frontend",
                        "Mode": {"Replicated": {"Replicas": 3}},
                        "TaskTemplate": {"ContainerSpec": {"Image": "nginx:alpine@sha256:1234"}},
                    },
                    "Endpoint": {"Ports": [{"PublishedPort": 80, "TargetPort": 80, "Protocol": "tcp"}]},
                }
            ]
        ).encode(),
    ),
    "/services/frontend": (
        200,
        "application/json",
        json.dumps({"ID": "svc1234567890abcdef", "Spec": {"Name": "frontend"}}).encode(),
    ),
    "/tasks": (
        200,
        "application/json",
        json.dumps(
            [
                {
                    "ID": "task1234567890abcdef",
                    "ServiceID": "svc1234567890abcdef",
                    "NodeID": "node1234567890abcdef",
                    "Slot": 1,
                    "DesiredState": "running",
                    "Status": {"State": "running", "Timestamp": "2026-01-01T10:20:30.123456Z"},
                    "Spec": {"ContainerSpec": {"Image": "nginx:alpine@sha256:1234"}},
                }
            ]
        ).encode(),
    ),
    "/tasks/task1234567890abcdef": (
        200,
        "application/json",
        json.dumps({"ID": "task1234567890abcdef", "DesiredState": "running"}).encode(),
    ),
    "/configs": (
        200,
        "application/json",
        json.dumps([{"ID": "cfg1234567890", "Spec": {"Name": "app.conf"}, "CreatedAt": "2026-01-01T00:00:00Z", "UpdatedAt": "2026-01-01T00:00:00Z"}]).encode(),
    ),
    "/secrets": (
        200,
        "application/json",
        json.dumps([{"ID": "sec1234567890", "Spec": {"Name": "db_pass"}, "CreatedAt": "2026-01-01T00:00:00Z", "UpdatedAt": "2026-01-01T00:00:00Z"}]).encode(),
    ),
    "/plugins": (
        200,
        "application/json",
        json.dumps([{"Id": "plg1234567890", "Name": "local-persist:latest", "Enabled": True, "Config": {"Description": "Local volume plugin"}}]).encode(),
    ),
}


def make_mux_frame(stream_type: int, text: str) -> bytes:
    payload = text.encode("utf-8")
    header = struct.pack(">BBBBi", stream_type, 0, 0, 0, len(payload))
    return header + payload


class MockDockerHandler(http.server.BaseHTTPRequestHandler):
    last_path: str = ""

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        MockDockerHandler.last_path = self.path
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/containers/web-api/logs" or path == "/services/frontend/logs":
            body = make_mux_frame(1, "line 1 stdout\n") + make_mux_frame(2, "line 2 stderr\n")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.docker.raw-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path in MOCK_ROUTES:
            status, ctype, body = MOCK_ROUTES[path]
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        err = json.dumps({"message": f"No such object: {path}"}).encode()
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(err)))
        self.end_headers()
        self.wfile.write(err)


class ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class ThreadedTCPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class DocatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.sock_path = os.path.join(cls.tmpdir.name, "docker.sock")
        cls.unix_server = ThreadedUnixServer(cls.sock_path, MockDockerHandler)
        cls.unix_thread = threading.Thread(target=cls.unix_server.serve_forever, daemon=True)
        cls.unix_thread.start()

        cls.tcp_server = ThreadedTCPServer(("127.0.0.1", 0), MockDockerHandler)
        cls.tcp_port = cls.tcp_server.server_address[1]
        cls.tcp_thread = threading.Thread(target=cls.tcp_server.serve_forever, daemon=True)
        cls.tcp_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.unix_server.shutdown()
        cls.unix_server.server_close()
        cls.tcp_server.shutdown()
        cls.tcp_server.server_close()
        cls.tmpdir.cleanup()

    def run_cli(self, argv: list[str]) -> str:
        parser = docat.build_parser()
        args = parser.parse_args(argv)
        client = docat.DockerClient(
            host=args.host,
            timeout=args.timeout,
            api_version=args.api_version,
        )
        return docat.execute_command(client, args)

    def test_unix_socket_containers_table_json_yaml(self) -> None:
        host = f"unix://{self.sock_path}"
        table_out = self.run_cli(["-H", host, "containers"])
        self.assertIn("CONTAINER ID", table_out)
        self.assertIn("web-api", table_out)
        self.assertIn("0.0.0.0:8080->80/tcp", table_out)

        json_out = self.run_cli(["-H", host, "containers", "-o", "json"])
        parsed = json.loads(json_out)
        self.assertEqual(parsed[0]["Image"], "nginx:alpine")

        yaml_out = self.run_cli(["-H", host, "containers", "-o", "yaml"])
        self.assertIn("Image: nginx:alpine", yaml_out)
        self.assertIn("- /web-api", yaml_out)

    def test_tcp_socket_and_shorthand_hosts(self) -> None:
        for host in (f"tcp://127.0.0.1:{self.tcp_port}", f"http://127.0.0.1:{self.tcp_port}", f"127.0.0.1:{self.tcp_port}"):
            out = self.run_cli(["-H", host, "ping"])
            self.assertEqual(out, "OK")

    def test_container_inspect_logs_top_stats_changes(self) -> None:
        host = self.sock_path
        inspect_yaml = self.run_cli(["-H", host, "container", "inspect", "web-api", "-o", "yaml"])
        self.assertIn("Name: /web-api", inspect_yaml)
        self.assertIn("Running: true", inspect_yaml)

        logs_out = self.run_cli(["-H", host, "container", "logs", "web-api"])
        self.assertEqual(logs_out, "line 1 stdout\nline 2 stderr")

        top_out = self.run_cli(["-H", host, "container", "top", "web-api"])
        self.assertIn("nginx: master process", top_out)

        stats_out = self.run_cli(["-H", host, "container", "stats", "web-api", "-o", "json", "--compact"])
        self.assertEqual(json.loads(stats_out)["memory_stats"]["usage"], 1048576)

        changes_out = self.run_cli(["-H", host, "container", "changes", "web-api"])
        self.assertIn("Modified (C)", changes_out)
        self.assertIn("/tmp/app.pid", changes_out)

    def test_images_networks_volumes(self) -> None:
        host = self.sock_path
        self.assertIn("nginx:alpine", self.run_cli(["-H", host, "images"]))
        self.assertIn("echo hi", self.run_cli(["-H", host, "image", "history", "nginx:alpine"]))
        self.assertIn("bridge", self.run_cli(["-H", host, "networks"]))
        self.assertIn("db-data", self.run_cli(["-H", host, "volumes"]))

    def test_swarm_nodes_services_tasks_configs_secrets_plugins(self) -> None:
        host = self.sock_path
        self.assertIn("swarm1234567890", self.run_cli(["-H", host, "swarm"]))
        self.assertIn("mgr-01", self.run_cli(["-H", host, "nodes"]))
        self.assertIn("manager (leader)", self.run_cli(["-H", host, "nodes"]))
        self.assertIn("frontend", self.run_cli(["-H", host, "services"]))
        self.assertIn("replicated (3)", self.run_cli(["-H", host, "services"]))
        self.assertIn("task12345678", self.run_cli(["-H", host, "tasks"]))
        self.assertIn("app.conf", self.run_cli(["-H", host, "configs"]))
        self.assertIn("db_pass", self.run_cli(["-H", host, "secrets"]))
        self.assertIn("local-persist:latest", self.run_cli(["-H", host, "plugins"]))

    def test_filters_and_query_params(self) -> None:
        host = self.sock_path
        self.run_cli(["-H", host, "containers", "-a", "-f", "status=running", "-f", "label=env=prod"])
        parsed = urllib.parse.urlparse(MockDockerHandler.last_path)
        qs = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(qs["all"], ["1"])
        filters = json.loads(qs["filters"][0])
        self.assertEqual(filters, {"status": ["running"], "label": ["env=prod"]})

    def test_top_level_inspect_fallback(self) -> None:
        host = self.sock_path
        # Inspects network "bridge" after container/image return 404
        out = self.run_cli(["-H", host, "inspect", "bridge", "-o", "json"])
        self.assertEqual(json.loads(out)["Name"], "bridge")


if __name__ == "__main__":
    unittest.main()
