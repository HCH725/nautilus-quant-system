from __future__ import annotations

from hashlib import sha256
from importlib.metadata import Distribution, distributions
from pathlib import Path
import re
import sys


_NORMALIZE_DISTRIBUTION = re.compile(r"[-_.]+")


def _distribution_name(value: str) -> str:
    return _NORMALIZE_DISTRIBUTION.sub("-", value).lower()


def _locked_distributions(path: Path) -> dict[str, tuple[str, str]]:
    locked: dict[str, tuple[str, str]] = {}
    for line in path.read_text().splitlines():
        requirement = line.strip()
        if not requirement:
            continue
        name, separator, version = requirement.partition("==")
        normalized = _distribution_name(name)
        if not separator or not normalized or not version or normalized in locked:
            raise ValueError("isolated research lock must contain unique exact pins")
        locked[normalized] = (name, version)
    if not locked:
        raise ValueError("isolated research lock is empty")
    return locked


def _installed_distributions(site_packages: Path) -> dict[str, Distribution]:
    installed: dict[str, Distribution] = {}
    for distribution in distributions(path=[str(site_packages)]):
        name = distribution.metadata["Name"]
        if not name:
            raise ValueError("isolated research distribution has no name")
        normalized = _distribution_name(name)
        if normalized in installed:
            raise ValueError("isolated research distribution name is ambiguous")
        installed[normalized] = distribution
    return installed


def _hash_file(digest, label: str, path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"isolated research runtime is missing: {path}")
    digest.update(label.encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())


def _hash_runtime_tree(digest, site_packages: Path, environment: Path) -> None:
    """Hash every behavior-bearing site-packages entry, including unowned startup files."""
    paths = sorted(site_packages.rglob("*"), key=lambda path: path.relative_to(site_packages).as_posix())
    for path in paths:
        relative = path.relative_to(site_packages)
        label = f"site-packages/{relative.as_posix()}"
        if path.is_symlink():
            try:
                target = path.resolve(strict=True)
                target.relative_to(environment)
            except (OSError, ValueError) as error:
                raise ValueError(f"isolated research symlink escapes environment: {path}") from error
            digest.update(f"symlink:{label}".encode())
            digest.update(b"\0")
            digest.update(str(path.readlink()).encode())
            digest.update(b"\0")
            if target.is_file():
                digest.update(target.read_bytes())
            elif not target.is_dir() or not target.is_relative_to(site_packages):
                raise ValueError(f"isolated research symlink target is unsupported: {path}")
        elif path.is_dir():
            continue
        elif path.is_file():
            _hash_file(digest, label, path)
        else:
            raise ValueError(f"isolated research runtime has unsupported file type: {path}")


def research_runtime_identity(repository_root: Path, *, require_active: bool = False) -> str:
    """Hash the exact locked PyBroker venv and optionally attest the active interpreter."""
    repository_root = Path(repository_root).resolve()
    environment = (repository_root / "research/.venv").resolve()
    site_packages = sorted((environment / "lib").glob("python*/site-packages"))
    if len(site_packages) != 1:
        raise ValueError("isolated research site-packages is ambiguous")
    site_packages_path = site_packages[0].resolve()
    lock_path = repository_root / "research/requirements.lock"
    config_path = environment / "pyvenv.cfg"
    python_path = (environment / "bin/python").resolve()
    if require_active and (
        Path(sys.executable).resolve() != python_path or Path(sys.prefix).resolve() != environment
    ):
        raise ValueError("active process is not the attested research interpreter")

    locked = _locked_distributions(lock_path)
    installed = _installed_distributions(site_packages_path)
    if installed.keys() != locked.keys():
        missing = sorted(locked.keys() - installed.keys())
        unexpected = sorted(installed.keys() - locked.keys())
        raise ValueError(
            f"isolated research distributions differ from lock: missing={missing}, unexpected={unexpected}",
        )

    digest = sha256()
    _hash_file(digest, "requirements.lock", lock_path)
    _hash_file(digest, "pyvenv.cfg", config_path)
    _hash_file(digest, "python", python_path)
    for normalized in sorted(locked):
        locked_name, locked_version = locked[normalized]
        distribution = installed[normalized]
        if distribution.version != locked_version:
            raise ValueError(
                f"isolated research distribution version differs from lock: {locked_name}",
            )
        files = distribution.files
        if files is None:
            raise ValueError(f"isolated research distribution has no file manifest: {locked_name}")
        digest.update(f"distribution:{normalized}=={locked_version}".encode())
        digest.update(b"\0")
        for package_path in sorted(files, key=str):
            path = Path(str(distribution.locate_file(package_path))).resolve()
            try:
                label = path.relative_to(environment).as_posix()
            except ValueError as error:
                raise ValueError(
                    f"isolated research distribution escapes environment: {locked_name}",
                ) from error
            _hash_file(digest, label, path)
    _hash_runtime_tree(digest, site_packages_path, environment)
    return digest.hexdigest()
