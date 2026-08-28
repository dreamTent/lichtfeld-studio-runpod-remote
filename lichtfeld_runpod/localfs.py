from __future__ import annotations

from pathlib import Path


IMAGE_DIRS = ("images", "image", "imgs", "rgb")
CONFIG_NAMES = ("config.json", "lichtfeld.json", "lichtfeld-studio.json")


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def list_local_dir(path: Path, home: Path) -> dict:
    path = path.expanduser()
    if not path.is_absolute():
        path = home / path
    path = path.resolve()
    if path != home.resolve() and not is_within(path, home):
        raise PermissionError("path is outside the home directory")
    if not path.exists():
        raise FileNotFoundError(str(path))
    if not path.is_dir():
        raise NotADirectoryError(str(path))
    entries = []
    for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name.startswith(".") and child.name not in {".", ".."}:
            continue
        entries.append(
            {
                "name": child.name,
                "path": str(child),
                "is_dir": child.is_dir(),
            }
        )
    scene, configs = inspect_dataset(path)
    return {
        "path": str(path),
        "parent": str(path.parent) if path != home.resolve() else str(path),
        "entries": entries,
        "is_scene": scene is not None,
        "scene_root": str(scene) if scene else None,
        "configs": configs,
    }


def inspect_dataset(root: Path) -> tuple[Path | None, list[str]]:
    """Find a COLMAP scene (images/ + sparse/) and nearby JSON configs."""
    root = root.resolve()
    if not root.is_dir():
        return None, []
    scene = _find_scene(root)
    if scene is None:
        return None, []
    configs: list[str] = []
    for p in sorted(scene.glob("*.json")):
        configs.append(p.name)
    for name in CONFIG_NAMES:
        hit = scene / name
        rel = name
        if hit.is_file() and rel not in configs:
            configs.append(rel)
    return scene, configs


def _find_scene(root: Path) -> Path | None:
    if _looks_like_scene(root):
        return root
    for sparse in root.rglob("sparse"):
        parent = sparse.parent
        if _looks_like_scene(parent):
            return parent
    return None


def _looks_like_scene(path: Path) -> bool:
    if not (path / "sparse").exists():
        return False
    return any((path / name).is_dir() for name in IMAGE_DIRS)
