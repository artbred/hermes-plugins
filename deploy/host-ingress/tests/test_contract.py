"""Source contract; no service/container changes. Run with unittest discover."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PORTS = {"api": 8642, "speech": 9120, "uploads": 9122, "push": 9123}
BACKENDS = {"api": 8642, "speech": 9119, "uploads": 9121, "push": 9123}
PIN = "nginx@sha256:1d13701a5f9f3fb01aaa88cef2344d65b6b5bf6b7d9fa4cf0dca557a8d7702ba"


class Contract(unittest.TestCase):
    def test_compose_security_and_dns(self):
        result = subprocess.run(["docker", "compose", "-f", str(ROOT / "compose.yaml"),
                                 "config", "--format", "json"], check=True, capture_output=True, text=True)
        config = json.loads(result.stdout)
        self.assertEqual(set(config["services"]), {"hermes-host-ingress"})
        service = config["services"]["hermes-host-ingress"]
        self.assertEqual(service["image"], PIN)
        self.assertEqual(service["user"], "65534:65534")
        self.assertTrue(service["read_only"])
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", service["security_opt"])
        self.assertFalse(service.get("ports"))
        self.assertNotIn("extra_hosts", service)
        self.assertNotIn("privileged", service)
        self.assertNotIn("network_mode", service)
        self.assertEqual(set(service["networks"]), {"dokploy-network"})
        self.assertIn("hermes-host-ingress", service["networks"]["dokploy-network"]["aliases"])
        self.assertTrue(config["networks"]["dokploy-network"]["external"])
        self.assertEqual(set(map(str, service["expose"])), set(map(str, PORTS.values())))
        self.assertTrue(service["tmpfs"])
        mounts = {v["target"]: v for v in service["volumes"]}
        sockets = mounts["/sockets"]
        self.assertEqual(sockets["source"], "/var/lib/hermes-host-ingress/sockets")
        self.assertTrue(sockets["read_only"])
        self.assertFalse(sockets.get("bind", {}).get("create_host_path", False))
        self.assertTrue(mounts["/etc/nginx/hermes-nginx.conf"]["read_only"])

    def test_nginx_routes_and_transparency(self):
        config = (ROOT / "nginx.conf").read_text()
        routes = re.findall(r"listen (\d+);\s*location / \{\s*proxy_pass http://unix:/sockets/(\w+)\.sock;", config)
        self.assertEqual({name: int(port) for port, name in routes}, PORTS)
        self.assertEqual(len(routes), 4)
        self.assertNotRegex(config, r"(?:\d{1,3}\.){3}\d{1,3}")
        self.assertIn("proxy_set_header Host $http_host;", config)
        self.assertIn("proxy_set_header Authorization $http_authorization;", config)
        self.assertIn("proxy_pass_request_headers on;", config)
        self.assertIn("proxy_buffering off;", config)
        self.assertIn("proxy_request_buffering off;", config)
        self.assertIn("proxy_http_version 1.1;", config)
        self.assertIn("client_max_body_size 0;", config)
        self.assertIn("proxy_redirect off;", config)
        self.assertIn("access_log off;", config)
        self.assertIn("proxy_next_upstream off;", config)
        self.assertNotIn("include", config)  # No stock default server/config.

    def test_host_units_and_permissions(self):
        socket = (ROOT / "systemd/hermes-host-ingress@.socket").read_text()
        service = (ROOT / "systemd/hermes-host-ingress@.service").read_text()
        self.assertIn("ListenStream=/var/lib/hermes-host-ingress/sockets/%i.sock", socket)
        for line in ("SocketUser=root", "SocketGroup=nogroup", "SocketMode=0660", "Accept=no"):
            self.assertIn(line, socket)
        self.assertIn("systemd-socket-proxyd ${BACKEND}", service)
        self.assertIn("IPAddressDeny=any", service)
        self.assertIn("IPAddressAllow=localhost", service)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET", service)
        self.assertNotIn("hermes-gateway.service", service)
        for name, port in BACKENDS.items():
            self.assertEqual((ROOT / f"backends/{name}.conf").read_text(), f"BACKEND=localhost4:{port}\n")
        tmpfiles = (ROOT / "tmpfiles/hermes-host-ingress.conf").read_text()
        self.assertIn("d /var/lib/hermes-host-ingress 0700 root root -", tmpfiles)
        self.assertIn("d /var/lib/hermes-host-ingress/sockets 0750 root nogroup -", tmpfiles)
        for path in ROOT.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                self.assertEqual(path.stat().st_mode & 0o022, 0, str(path))

    def test_cloud_init_alias_patch(self):
        patch = (ROOT / "hosts/localhost4.patch").read_text()
        self.assertIn("--- /etc/hosts", patch)
        self.assertIn("--- /etc/cloud/templates/hosts.debian.tmpl", patch)
        self.assertEqual(patch.count("+127.0.0.1 localhost4"), 2)
        # Numeric values are host name infrastructure only, never backend targets.
        for path in (ROOT / "backends").glob("*.conf"):
            self.assertNotRegex(path.read_text(), r"(?:\d{1,3}\.){3}\d{1,3}")

    def test_hosts_patch_applies_only_to_staging(self):
        with tempfile.TemporaryDirectory(prefix="host-ingress-hosts-test-",
                                        dir=os.environ.get("TMPDIR", "/root/.hermes/cache/scratch")) as temp:
            stage = Path(temp)
            (stage / "etc/cloud/templates").mkdir(parents=True)
            live_shape = "# cloud managed\n127.0.0.1\tlocalhost\n\n::1\tlocalhost ip6-localhost\n"
            template_shape = "## template:jinja\n127.0.1.1 {{fqdn}} {{hostname}}\n127.0.0.1 localhost\n\n::1 localhost ip6-localhost\n"
            (stage / "etc/hosts").write_text(live_shape)
            (stage / "etc/cloud/templates/hosts.debian.tmpl").write_text(template_shape)
            subprocess.run(["patch", "--batch", "--forward", "--fuzz=0", "-l", "-p1", "-d", str(stage),
                            "-i", str(ROOT / "hosts/localhost4.patch")], check=True, capture_output=True)
            self.assertEqual((stage / "etc/hosts").read_text(),
                             live_shape.replace("127.0.0.1\tlocalhost\n", "127.0.0.1\tlocalhost\n127.0.0.1 localhost4\n"))
            self.assertEqual((stage / "etc/cloud/templates/hosts.debian.tmpl").read_text(),
                             template_shape.replace("127.0.0.1 localhost\n", "127.0.0.1 localhost\n127.0.0.1 localhost4\n"))


if __name__ == "__main__":
    unittest.main()
