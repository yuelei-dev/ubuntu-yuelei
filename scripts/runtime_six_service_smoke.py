#!/usr/bin/env python3
"""Start the six runtime services only against disposable local state."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


SERVICES = (
    ("content", "content_api.py", 8096, "/api/gen/health", "CONTENT_API_PORT"),
    ("auth", "auth_server.py", 8095, "/api/auth/health", None),
    ("dl", "dl_service.py", 8097, "/api/gen/dl/health", None),
    ("imggen", "imggen_api.py", 8101, "/api/gen/banana/health", "IMGGEN_API_PORT"),
    ("leadgen", "leadgen_api.py", 8100, "/api/gen/leadgen/health", "LEADGEN_API_PORT"),
    ("admin", "admin_api.py", 8098, "/api/admin/health", "ADMIN_API_PORT"),
)


def port_available(port):
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    release = args.release.resolve()
    server = release / "server"
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    state = evidence / "external-state"
    state.mkdir(parents=True, exist_ok=True)
    unavailable = [port for _, _, port, _, _ in SERVICES if not port_available(port)]
    if unavailable:
        print(json.dumps({"error": "ports_unavailable", "ports": unavailable}))
        return 2

    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(server),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HF_HUB_OFFLINE": "1",
            "ADMIN_DB": str(state / "admin.db"),
            "AUTH_DB": str(state / "auth.db"),
            "CONTENT_ASSET_DB": str(state / "assets.db"),
            "CONTENT_DB": str(state / "content.db"),
            "CONTENT_JOB_DB": str(server / "content_jobs.db"),
            "CONTENT_OUT": str(state / "content_out"),
            "DIGITAL_IP_DB": str(state / "digital_ip.db"),
            "FEATURE_FLAGS_DB": str(state / "flags.db"),
            "TIKHUB_CACHE_DB": str(state / "tikhub.db"),
            "ADMIN_API_PORT": "8098",
            "CONTENT_API_PORT": "8096",
            "IMGGEN_API_PORT": "8101",
            "LEADGEN_API_PORT": "8100",
        }
    )
    for key in tuple(env):
        if any(word in key.upper() for word in ("API_KEY", "SECRET", "PASSWORD", "TOKEN")):
            env.pop(key, None)

    processes = []
    handles = []
    results = []
    try:
        for name, script, port, path, port_env in SERVICES:
            service_env = env.copy()
            if port_env:
                service_env[port_env] = str(port)
            handle = (evidence / f"{name}.log").open("wb")
            handles.append(handle)
            process = subprocess.Popen(
                [sys.executable, str(server / script)],
                cwd=server,
                env=service_env,
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
            processes.append((name, process))
            if name == "content":
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        with urllib.request.urlopen(
                            "http://127.0.0.1:8096/api/gen/health", timeout=1
                        ) as response:
                            if response.status == 200:
                                break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.2)

        deadline = time.monotonic() + 30
        pending = {name: (port, path) for name, _, port, path, _ in SERVICES}
        while pending and time.monotonic() < deadline:
            for name, (port, path) in list(pending.items()):
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}{path}", timeout=1
                    ) as response:
                        results.append({"service": name, "status": response.status})
                        pending.pop(name)
                except urllib.error.HTTPError as error:
                    results.append({"service": name, "status": error.code})
                    pending.pop(name)
                except (OSError, urllib.error.URLError):
                    pass
            if pending:
                time.sleep(0.25)
        for name in pending:
            process = dict(processes)[name]
            results.append(
                {"service": name, "status": None, "process_exit": process.poll()}
            )
    finally:
        for _, process in processes:
            if process.poll() is None:
                process.terminate()
        for _, process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for handle in handles:
            handle.close()

    payload = {
        "server_role": "local-isolation",
        "paid_model_calls": 0,
        "results": sorted(results, key=lambda item: item["service"]),
        "all_healthy": len(results) == 6
        and all(
            item["status"] == 200
            or (item["service"] == "admin" and item["status"] == 401)
            for item in results
        ),
        "processes_stopped": all(process.poll() is not None for _, process in processes),
    }
    (evidence / "six-service-smoke.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["all_healthy"] and payload["processes_stopped"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
