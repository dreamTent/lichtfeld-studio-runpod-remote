from __future__ import annotations

from pathlib import Path

from .storage import is_archive_path


IMAGE_DIRS = ("images", "image", "imgs", "rgb")
CONFIG_NAMES = ("config.json", "lichtfeld.json", "lichtfeld-studio.json")
DATASETS_DIRNAME = "datasets"
ARCHIVE_FILETYPES = [
    ("Dataset archives", "*.tar *.zip *.tgz *.gz"),
    ("All files", "*.*"),
]
JSON_FILETYPES = [
    ("JSON config", "*.json"),
    ("All files", "*.*"),
]


def ensure_datasets_dir(workdir: Path) -> Path:
    path = workdir / DATASETS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def is_allowed_path(path: Path, home: Path, workdir: Path) -> bool:
    path = path.resolve()
    home = home.resolve()
    workdir = workdir.resolve()
    if path == home or is_within(path, home):
        return True
    if path == workdir or is_within(path, workdir):
        return True
    return False


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def list_local_dir(path: Path, home: Path, workdir: Path | None = None) -> dict:
    home = home.resolve()
    workdir = (workdir or home).resolve()
    path = path.expanduser()
    if not path.is_absolute():
        path = workdir / path
    path = path.resolve()
    if not is_allowed_path(path, home, workdir):
        raise PermissionError("path is outside the home and working directories")
    if not path.exists():
        raise FileNotFoundError(str(path))
    if not path.is_dir():
        raise NotADirectoryError(str(path))
    parent_path = path.parent
    if path == home or not is_allowed_path(parent_path, home, workdir):
        parent = str(path)
    else:
        parent = str(parent_path)
    entries = []
    for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name.startswith(".") and child.name not in {".", ".."}:
            continue
        is_file = child.is_file()
        entries.append(
            {
                "name": child.name,
                "path": str(child),
                "is_dir": child.is_dir(),
                "is_archive": is_file and is_archive_path(child),
                "is_json": is_file and child.suffix.lower() == ".json",
            }
        )
    scene, configs = inspect_dataset(path)
    return {
        "path": str(path),
        "parent": parent,
        "entries": entries,
        "is_scene": scene is not None,
        "scene_root": str(scene) if scene else None,
        "configs": configs,
    }


def native_picker_available() -> bool:
    try:
        import tkinter  # noqa: F401
    except Exception:
        return False
    return True


def pick_local_path(
    kind: str,
    start: Path,
    filetypes: list[tuple[str, str]] | None = None,
) -> str | None:
    import tkinter as tk
    from tkinter import filedialog

    initial = start if start.is_dir() else start.parent
    if not initial.exists():
        initial = Path.home()
    root = tk.Tk()
    root.withdraw()
    try:
        root.wm_attributes("-topmost", True)
    except Exception:
        pass
    try:
        root.update()
        if kind == "dir":
            chosen = filedialog.askdirectory(initialdir=str(initial), parent=root)
        else:
            chosen = filedialog.askopenfilename(
                initialdir=str(initial),
                filetypes=filetypes or [("All files", "*.*")],
                parent=root,
            )
    finally:
        root.destroy()
    return str(chosen) if chosen else None


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
