from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .log import log

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

class RunpodError(Exception):
    pass


class RunpodClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def _headers(self, content: bool = False) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if content:
            h["Content-Type"] = "application/json"
        return h

    def request(self, method: str, url: str, payload: Any = None, timeout: int = 90) -> tuple[int, Any]:
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers=self._headers(payload is not None), method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                body = json.loads(raw) if raw else None
                return resp.status, body
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = raw
            return e.code, body
        except urllib.error.URLError as e:
            raise RunpodError(f"request failed {url}: {e}") from e

    def ensure_ssh_key(self, pubkey: str) -> None:
        status, existing = self.request("GET", "https://api.runpod.io/v2/account/ssh-keys")
        log("runpod", f"ssh-keys GET {status}")
        keys: list[str] = []
        if isinstance(existing, dict):
            keys = list(existing.get("keys") or [])
        if pubkey not in keys:
            keys.append(pubkey)
            for payload in ({"keys": keys}, keys):
                status, body = self.request("PUT", "https://api.runpod.io/v2/account/ssh-keys", payload)
                log("runpod", f"ssh-keys PUT {status}")
                if status in (200, 201, 204):
                    return
            raise RunpodError(f"failed to register SSH key: {body}")

    def create_pod(
        self,
        *,
        name: str,
        image: str,
        gpu: str,
        gpu_count: int,
        cloud: str,
        disk_gb: int,
        volume_gb: int,
        volume_mount: str,
        pubkey: str,
        allowed_cuda: list[str],
    ) -> dict[str, Any]:
        clouds = ["SECURE", "COMMUNITY"] if cloud == "AUTO" else [cloud]
        last: Any = None
        for c in clouds:
            payload: dict[str, Any] = {
                "name": name,
                "imageName": image,
                "gpuTypeIds": [gpu],
                "gpuTypePriority": "custom",
                "gpuCount": gpu_count,
                "cloudType": c,
                "computeType": "GPU",
                "containerDiskInGb": disk_gb,
                "volumeInGb": volume_gb,
                "volumeMountPath": volume_mount,
                "ports": ["22/tcp", "8888/http"],
                "supportPublicIp": True,
                "interruptible": False,
                "env": {"PUBLIC_KEY": pubkey},
            }
            if allowed_cuda:
                payload["allowedCudaVersions"] = allowed_cuda
            log("runpod", f"creating pod gpu={gpu} cloud={c}")
            status, body = self.request("POST", "https://rest.runpod.io/v1/pods", payload)
            last = body
            if status in (200, 201) and isinstance(body, dict) and body.get("id"):
                log("runpod", f"created {body['id']} cost={body.get('costPerHr')}/hr")
                return body
            log("runpod", f"create failed {status} {str(body)[:400]}")
            if allowed_cuda and status not in (200, 201):
                payload.pop("allowedCudaVersions", None)
                status, body = self.request("POST", "https://rest.runpod.io/v1/pods", payload)
                last = body
                if status in (200, 201) and isinstance(body, dict) and body.get("id"):
                    log("runpod", f"created {body['id']} (without CUDA filter)")
                    return body
        raise RunpodError(f"could not place a {gpu} pod: {last}")

    def create_pod_retry(
        self,
        *,
        should_stop: Any = None,
        on_attempt: Any = None,
        on_wait: Any = None,
        delay: float = 15,
        **kwargs: Any,
    ) -> dict[str, Any]:
        attempt = 0
        while True:
            if should_stop and should_stop():
                raise RunpodError("pod create cancelled")
            attempt += 1
            try:
                return self.create_pod(**kwargs)
            except RunpodError as e:
                msg = f"attempt {attempt}: {e}"
                log("runpod", msg)
                if on_attempt:
                    on_attempt(msg)
                if on_wait:
                    on_wait(delay)
                time.sleep(delay)

    def get_pod(self, pod_id: str) -> dict[str, Any]:
        status, body = self.request("GET", f"https://api.runpod.io/v2/pods/{pod_id}")
        if status == 200 and isinstance(body, dict):
            return body
        status, body = self.request("GET", f"https://rest.runpod.io/v1/pods/{pod_id}")
        if status == 200 and isinstance(body, dict):
            return body
        raise RunpodError(f"pod lookup failed {status} {body}")

    def list_pods(self) -> list[dict[str, Any]]:
        status, body = self.request("GET", "https://api.runpod.io/v2/pods")
        pods = _as_pod_list(body)
        if status == 200 and pods is not None:
            return pods
        status, body = self.request("GET", "https://rest.runpod.io/v1/pods")
        pods = _as_pod_list(body)
        if status == 200 and pods is not None:
            return pods
        raise RunpodError(f"list pods failed {status} {body}")

    def list_gpus(self) -> list[dict[str, Any]]:
        qs = urllib.parse.urlencode({"include": "AVAILABILITY", "product": "POD"})
        status, body = self.request("GET", f"https://api.runpod.io/v2/catalog/gpus?{qs}")
        if status != 200:
            raise RunpodError(f"gpu catalog failed {status} {body}")
        items: Any = body
        if isinstance(body, dict):
            items = body.get("gpus") or body.get("items") or body.get("data") or []
        if not isinstance(items, list):
            return []
        out: list[dict[str, Any]] = []
        for g in items:
            if not isinstance(g, dict):
                continue
            gid = str(g.get("id") or g.get("displayName") or g.get("name") or "")
            if not gid:
                continue
            avail = g.get("availability") or {}
            if not isinstance(avail, str):
                avail = str(
                    (avail or {}).get("status")
                    or (avail or {}).get("secure")
                    or g.get("stockStatus")
                    or ""
                )
            price = g.get("price") if isinstance(g.get("price"), dict) else {}
            memory = g.get("memory") or g.get("memoryInGb") or g.get("memoryInGB")
            out.append(
                {
                    "id": gid,
                    "name": str(g.get("name") or g.get("displayName") or gid),
                    "memory_gb": memory,
                    "availability": str(avail),
                    "secure_price": (price or {}).get("secure") if isinstance(price, dict) else g.get("securePrice"),
                    "community_price": (price or {}).get("community") if isinstance(price, dict) else g.get("communityPrice"),
                }
            )
        out.sort(key=lambda x: str(x["name"]).lower())
        return out

    def wait_ssh(
        self,
        pod_id: str,
        *,
        should_stop: Any = None,
        on_wait: Any = None,
    ) -> tuple[str, int] | None:
        i = 0
        while True:
            try:
                pod = self.get_pod(pod_id)
            except RunpodError as e:
                log("runpod", f"[{i}] pod lookup failed: {e}")
                i += 1
                if should_stop and should_stop(i):
                    return None
                if on_wait:
                    on_wait(5)
                time.sleep(5)
                continue
            endpoint = ssh_endpoint(pod)
            log("runpod", f"[{i}] status={pod.get('status')} ssh={endpoint}")
            if endpoint:
                return endpoint
            i += 1
            if should_stop and should_stop(i):
                return None
            if on_wait:
                on_wait(5)
            time.sleep(5)

    def terminate(self, pod_id: str) -> None:
        status, body = self.request("DELETE", f"https://rest.runpod.io/v1/pods/{pod_id}")
        log("runpod", f"terminate {pod_id} -> {status}")
        if status in (200, 204, 404):
            return
        log("runpod", f"terminate body {str(body)[:300]}")
        raise RunpodError(f"terminate {pod_id} failed {status} {body}")

    def pod_running(self, pod_id: str) -> bool:
        try:
            pod = self.get_pod(pod_id)
        except RunpodError:
            return False
        return pod_is_running(pod)


def ssh_endpoint(pod: dict[str, Any]) -> tuple[str, int] | None:
    ssh = pod.get("ssh") or {}
    direct = ssh.get("direct") if isinstance(ssh, dict) else None
    host, port = None, None
    if isinstance(direct, dict):
        host = direct.get("host") or direct.get("ip")
        port = direct.get("port")
    elif isinstance(direct, str) and ":" in direct:
        host, _, port_s = direct.rpartition(":")
        port = port_s
    runtime = pod.get("runtime") or {}
    ports = runtime.get("ports") if isinstance(runtime, dict) else None
    public_ip = pod.get("publicIp") or pod.get("public_ip")
    if not host and isinstance(ports, list):
        for p in ports:
            if isinstance(p, dict) and int(p.get("private") or p.get("privatePort") or 0) == 22:
                host = p.get("ip") or public_ip
                port = p.get("public") or p.get("publicPort")
    if not host and isinstance(ports, dict):
        pub = ports.get("22") or ports.get("22/tcp")
        if pub:
            host = public_ip
            port = pub
    if host and port:
        return str(host), int(port)
    return None


def pod_is_running(pod: dict[str, Any]) -> bool:
    status = str(pod.get("status") or pod.get("desiredStatus") or "").upper()
    return status in {"RUNNING"}


def _as_pod_list(body: Any) -> list[dict[str, Any]] | None:
    if isinstance(body, list):
        return [p for p in body if isinstance(p, dict)]
    if isinstance(body, dict):
        for key in ("pods", "data", "items"):
            val = body.get(key)
            if isinstance(val, list):
                return [p for p in val if isinstance(p, dict)]
    return None
