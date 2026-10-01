"""Exercise the local API from project creation through returned artifacts."""

from __future__ import annotations

import argparse
import time

import httpx


def run(base_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30) as client:
        projects = client.get("/api/v1/projects").raise_for_status().json()
        project = next((item for item in projects if item["slug"] == "local-fixture"), None)
        if project is None:
            project = (
                client.post(
                    "/api/v1/projects",
                    json={"slug": "local-fixture", "name": "Local fixture"},
                )
                .raise_for_status()
                .json()
            )
        task = (
            client.post(
                "/api/v1/tasks",
                json={
                    "project_id": project["id"],
                    "prompt": "Calculate a deterministic placeholder and write result.json.",
                },
            )
            .raise_for_status()
            .json()
        )
        for _ in range(120):
            task = client.get(f"/api/v1/tasks/{task['id']}").raise_for_status().json()
            if task["status"] not in {"RECEIVED", "PLANNING", "QUEUED", "RUNNING"}:
                break
            time.sleep(1)
        if task["status"] != "SUCCEEDED":
            raise RuntimeError(task)
        print(f"{task['id']}: {task['result']['summary']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8080")
    arguments = parser.parse_args()
    run(arguments.base_url)
