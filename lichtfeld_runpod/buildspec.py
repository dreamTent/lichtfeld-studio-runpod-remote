from __future__ import annotations

import re
from datetime import datetime

DEFAULT_GIT_REF = "v0.5.3"
DEFAULT_REPO = "https://github.com/MrNeRF/LichtFeld-Studio.git"
CMAKE_INSTALLER_VERSION = "3.31.6"

# First successful train used SM 89 (L40S). Rebuild when the GPU family changes.
GPU_CUDA_ARCH: dict[str, str] = {
    "NVIDIA L40S": "89",
    "NVIDIA L40": "89",
    "NVIDIA RTX 6000 Ada": "89",
    "NVIDIA RTX 5880 Ada": "89",
    "NVIDIA RTX 5000 Ada": "89",
    "NVIDIA GeForce RTX 4090": "89",
    "NVIDIA GeForce RTX 4080": "89",
    "NVIDIA A40": "86",
    "NVIDIA A6000": "86",
    "NVIDIA RTX A6000": "86",
    "NVIDIA RTX A5000": "86",
    "NVIDIA A5000": "86",
    "NVIDIA A100": "80",
    "NVIDIA A100 80GB PCIe": "80",
    "NVIDIA A100-SXM4-80GB": "80",
    "NVIDIA A100 80GB": "80",
    "NVIDIA H100": "90",
    "NVIDIA H100 80GB HBM3": "90",
    "NVIDIA H100 PCIe": "90",
    "NVIDIA H100 NVL": "90",
    "NVIDIA H200": "90",
    "NVIDIA RTX 5090": "120",
    "NVIDIA RTX PRO 6000": "120",
    "NVIDIA B200": "100",
}

_REF_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")
_ARCH_RE = re.compile(r"^[0-9]{2,3}(-real)?$")
_REPO_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+(?:\.git)?$")


def cuda_arch_for_gpu(gpu: str) -> str:
    name = (gpu or "").strip()
    if name in GPU_CUDA_ARCH:
        return GPU_CUDA_ARCH[name]
    upper = name.upper()
    if any(tok in upper for tok in ("L40", "4090", "4080", "ADA")):
        return "89"
    if "H200" in upper or "H100" in upper:
        return "90"
    if "B200" in upper or "BLACKWELL" in upper:
        return "100"
    if "5090" in upper or "PRO 6000" in upper:
        return "120"
    if "A100" in upper:
        return "80"
    if any(tok in upper for tok in ("A40", "A6000", "A5000", "3090", "3080")):
        return "86"
    return "89"


def gpu_slug(gpu: str) -> str:
    s = re.sub(r"(?i)^nvidia\s+", "", (gpu or "").strip())
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "gpu"


def version_slug(git_ref: str) -> str:
    s = (git_ref or "").strip()
    if len(s) > 1 and s[0] in "vV" and s[1].isdigit():
        s = s[1:]
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-.")
    return s or "dev"


def default_archive_name(git_ref: str, gpu: str, cuda_arch: str) -> str:
    return f"lichtfeld-{version_slug(git_ref)}-{gpu_slug(gpu)}-sm{cuda_arch}.tar.gz"


def default_build_folder(
    git_ref: str,
    gpu: str,
    cuda_arch: str,
    when: datetime | None = None,
) -> str:
    stamp = (when or datetime.now()).strftime("%y%m%d")
    folder = f"lichtfeld-{version_slug(git_ref)}-{gpu_slug(gpu)}-sm{cuda_arch}-{stamp}"
    return f"lichtfeld-builds/{folder}"


def normalize_git_ref(value: str) -> str:
    ref = (value or "").strip() or DEFAULT_GIT_REF
    if not _REF_RE.fullmatch(ref):
        raise ValueError("git ref must be a tag or branch name (letters, digits, . _ / -)")
    return ref


def normalize_cuda_arch(value: str, gpu: str = "") -> str:
    arch = (value or "").strip() or cuda_arch_for_gpu(gpu)
    if not _ARCH_RE.fullmatch(arch):
        raise ValueError("CUDA architecture must look like 89 or 89-real")
    return arch


def normalize_repo_url(value: str) -> str:
    url = (value or "").strip() or DEFAULT_REPO
    if url.endswith("/"):
        url = url[:-1]
    if not url.endswith(".git"):
        url = url + ".git"
    if not _REPO_RE.fullmatch(url):
        raise ValueError("repo must be an https://github.com/owner/name URL")
    return url
