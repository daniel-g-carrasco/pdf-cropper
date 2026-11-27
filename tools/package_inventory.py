#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Record the provenance and original notices of the files collected on Windows."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import shutil
import tempfile
from zipfile import BadZipFile, ZipFile
from pathlib import Path, PurePosixPath, PureWindowsPath


class InventoryError(ValueError):
    pass


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or PureWindowsPath(name).drive or "\\" in name or ".." in path.parts:
        raise InventoryError(f"unsafe package path: {name!r}")
    return path


def within(path: Path, root: Path) -> str | None:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def fields(path: Path) -> dict[str, list[str]]:
    parsed = {}
    for block in path.read_text(encoding="utf-8").split("\n\n"):
        lines = block.strip().splitlines()
        if lines and lines[0].startswith("%") and lines[0].endswith("%"):
            parsed[lines[0]] = lines[1:]
    return parsed


def pacman_database(root: Path):
    db = root / "var/lib/pacman/local"
    if not db.is_dir():
        raise InventoryError(f"pacman database is absent: {db}")
    packages, owners = {}, {}
    for directory in sorted(db.iterdir()):
        desc, listed = directory / "desc", directory / "files"
        if not desc.is_file() or not listed.is_file():
            continue
        meta = fields(desc)
        name, version = meta["%NAME%"][0], meta["%VERSION%"][0]
        names = [str(relative_path(item)) for item in fields(listed).get("%FILES%", []) if not item.endswith("/")]
        notices = [item for item in names if "/share/licenses/" in "/" + item]
        packages[name] = {
            "kind": "msys2", "name": name, "version": version, "base": meta.get("%BASE%", [name])[0],
            "licenses": meta.get("%LICENSE%", []), "license_files": [], "notice_inputs": notices,
        }
        for item in names:
            key = item.casefold()
            if key in owners and owners[key] != name:
                raise InventoryError(f"multiple pacman owners for {item}")
            owners[key] = name
    if not owners:
        raise InventoryError(f"pacman database has no file ownership records: {db}")
    return packages, owners


def wheel_origins():
    wheels, owners = {}, {}
    for dist in importlib.metadata.distributions():
        files = dist.files or ()
        name = dist.metadata.get("Name")
        if not name or not files:
            continue
        name = name.lower()
        notices = []
        for item in files:
            path = Path(dist.locate_file(item)).resolve()
            owners[path] = (name, item.as_posix())
            if (item.name.lower().startswith(("license", "licence", "copying", "notice"))
                    or "licenses" in item.parts):
                if path.is_file():
                    notices.append((path, item.name))
        license_name = dist.metadata.get("License-Expression") or dist.metadata.get("License")
        wheels[name] = ({"kind": "wheel", "name": name, "version": dist.version,
                         "licenses": [license_name] if license_name else [], "license_files": []}, notices)
    return wheels, owners


def upstream_notices(project_root, package):
    """Use original notices verified against the matching signed source."""
    prefix = "mingw-w64-ucrt-x86_64-"
    name, version = package["name"], package["version"]
    if not name.startswith(prefix):
        return []
    folder = project_root / "LICENSES/upstream" / relative_path(name.removeprefix(prefix))
    folder /= relative_path(version.replace(":", "~"))
    provenance = folder / "PROVENANCE.json"
    if not provenance.is_file():
        return []
    receipt = json.loads(provenance.read_bytes())
    if (receipt["package"], receipt["version"]) != (name, version):
        raise InventoryError(f"upstream notice receipt does not match {name} {version}")
    notices = []
    for item in receipt["files"]:
        path = folder / relative_path(item["file"])
        if sha256(path) != item["sha256"]:
            raise InventoryError(f"upstream notice changed: {path}")
        notices.append((path, item["file"]))
    return notices


def inventory(toc, msys_root: Path, project_root: Path, work: Path, *, revision="unrecorded", expected_versions=None):
    """Build the inventory and PyInstaller data entries from Analysis' table of contents."""
    msys_root, project_root, work = (Path(path).resolve() for path in (msys_root, project_root, work))
    packages, pacman = pacman_database(msys_root)
    versions = expected_versions or {}
    wheels, wheel_files = {}, None
    records, components, notices, issues = [], {}, {}, []
    for destination, source, kind in toc:
        path = Path(source).resolve()
        is_module = kind in ("PYMODULE", "PYSOURCE")
        target = ("module:" + destination if is_module else
                  "_internal/" + str(relative_path(destination.replace("\\", "/"))))
        installed = within(path, msys_root)
        known = pacman.get(installed.casefold()) if installed else None
        generated = within(path, work)
        own = within(path, project_root)
        component, origin, original_notices = None, path.name, []
        if known:
            stored = packages[known]
            component = {key: value for key, value in stored.items() if key != "notice_inputs"}
            required = versions.get(known, component["version"])
            if required != component["version"]:
                raise InventoryError(f"{known}: installed {component['version']}, expected {required}")
            origin = installed
            original_notices = [(msys_root / item, item.split("/share/licenses/", 1)[-1])
                                for item in stored["notice_inputs"]]
            original_notices = original_notices or upstream_notices(project_root, component)
        elif generated is not None:
            component = {"kind": "generated", "name": "pyinstaller", "version": revision}
            origin = generated
        elif any(part.casefold() in ("site-packages", "dist-packages") for part in path.parts):
            if wheel_files is None:
                wheels, wheel_files = wheel_origins()
            if path in wheel_files:
                owner, origin = wheel_files[path]
                component, original_notices = wheels[owner]
        elif own is not None and installed is None:
            component = {"kind": "project", "name": "project", "version": revision,
                         "license_files": []}
            origin = own
            original_notices = [(project_root / "LICENSE", "LICENSE")]
        if component is None:
            issues.append({"target": target, "reason": "no recorded origin"})
        key = f"{component['kind']}:{component['name']}" if component else None
        if component and key not in components:
            components[key] = component
            license_folder = f"licenses/{component['kind']}/{component['name']}"
            if component["kind"] != "generated" and not original_notices:
                issues.append({"target": key, "reason": "no original license text in the source metadata"})
            for notice, name in original_notices:
                if not notice.is_file():
                    issues.append({"target": key, "reason": f"original notice missing: {name}"})
                    continue
                dest = f"{license_folder}/{relative_path(name)}"
                if dest in notices and notices[dest] != notice:
                    raise InventoryError(f"conflicting notice files at {dest}")
                notices[dest] = notice
                component["license_files"].append("_internal/" + dest)
        records.append({"target": target, "component": key, "source": origin, "sha256": sha256(path)})
    manifest = {"schema_version": 1, "platform": "windows-x64", "components": components,
                "files": sorted(records, key=lambda row: row["target"]),
                "notices": {"_internal/" + name: sha256(path) for name, path in sorted(notices.items())},
                "issues": issues}
    work.mkdir(parents=True, exist_ok=True)
    listing = work / "PACKAGES.txt"
    listing.write_text(package_listing(components), encoding="utf-8", newline="\n")
    output = work / "inventory.json"
    output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    datas = [(name, str(path), "DATA") for name, path in sorted(notices.items())]
    datas += [("licenses/msys2/PACKAGES.txt", str(listing), "DATA"), ("licenses/inventory.json", str(output), "DATA")]
    return manifest, datas


def package_listing(components):
    lines = ["MSYS2 packages collected by PyInstaller", ""]
    for item in sorted((item for item in components.values() if item["kind"] == "msys2"),
                       key=lambda item: item["name"]):
        arc = f"{item['base']}-{item['version'].replace(':', '~')}.src.tar.zst"
        lines += [f"{item['name']} {item['version']}", "  License: " + ", ".join(item["licenses"]),
                  f"  Source archive: https://repo.msys2.org/mingw/sources/{arc}",
                  *(f"  {path}" for path in item["license_files"]), ""]
    return "\n".join(lines)


def verify_bundle(bundle: Path, manifest: dict, *, strict=False) -> None:
    if manifest.get("schema_version") != 1:
        raise InventoryError("unsupported inventory")
    sealed = manifest.get("payload")
    if sealed is not None and payload(bundle) != sealed:
        raise InventoryError("bundle file list or digest differs from sealed inventory")
    expected = dict(manifest["notices"])
    for row in manifest["files"]:
        if not row["target"].startswith("module:"):
            expected[row["target"]] = row["sha256"]
    for name, digest in expected.items():
        file = bundle / relative_path(name)
        got = sealed.get(name) if sealed is not None else sha256(file) if file.is_file() else None
        if got != digest:
            raise InventoryError(f"bundle content differs from inventory: {name}")
    if strict and manifest["issues"]:
        raise InventoryError(f"{len(manifest['issues'])} unresolved provenance or license records")


MANIFEST_PATH = "_internal/licenses/inventory.json"


def payload(bundle):
    return {path.relative_to(bundle).as_posix(): sha256(path)
            for path in sorted(bundle.rglob("*"))
            if path.is_file() and path.relative_to(bundle).as_posix() != MANIFEST_PATH}


def seal_bundle(bundle, manifest, *, executable, msys_root=None):
    """Reconcile the inventory with the files after staging and record the complete payload."""
    current = payload(bundle)
    existing = {row["target"]: row for row in manifest["files"]}
    originals = [row for target, row in existing.items() if target in current or target.startswith("module:")]
    issues = [row for row in manifest["issues"] if row["target"] in current or row["target"] in manifest["components"]]
    toc = []
    for name, checksum in current.items():
        old = existing.get(name)
        if (old and old["sha256"] == checksum) or name in manifest["notices"]:
            continue
        if name == "_internal/licenses/msys2/PACKAGES.txt":
            continue
        if name == executable or name in (
                "_internal/share/icons/Adwaita/icon-theme.cache", "_internal/share/icons/hicolor/icon-theme.cache"):
            if old:
                originals.remove(old)
            manifest["components"].setdefault("generated:staging", {"kind": "generated", "name": "staging"})
            originals.append({"target": name, "component": "generated:staging", "source": name, "sha256": checksum})
            continue
        if old:
            raise InventoryError(f"staging changed a collected file: {name}")
        source = msys_root / "ucrt64" / relative_path(name.removeprefix("_internal/")) if msys_root else None
        if source and source.is_file() and sha256(source) == checksum:
            toc.append((name.removeprefix("_internal/"), str(source), "DATA"))
        else:
            issues.append({"target": name, "reason": "no source for a staged file"})
    if toc:
        with tempfile.TemporaryDirectory(prefix="inventory-") as work:
            extra, assets = inventory(toc, msys_root, bundle, Path(work))
            for name, path, _type in assets:
                target = "_internal/" + name
                if target not in extra["notices"]:
                    continue
                dest = bundle / relative_path(target)
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, dest)
            originals += extra["files"]
            issues += extra["issues"]
            manifest["notices"].update(extra["notices"])
            for key, value in extra["components"].items():
                manifest["components"].setdefault(key, value)
    manifest["files"] = sorted(originals, key=lambda row: row["target"])
    manifest["issues"] = issues
    used = {row["component"] for row in originals}
    manifest["components"] = {key: value for key, value in manifest["components"].items()
                              if key in used or value.get("license_files")}
    listing = bundle / "_internal/licenses/msys2/PACKAGES.txt"
    listing.write_text(package_listing(manifest["components"]), encoding="utf-8", newline="\n")
    manifest["payload"] = payload(bundle)
    (bundle / MANIFEST_PATH).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest


def verify_portable(path, expected_inventory=None):
    with ZipFile(path) as archive:
        files = [entry for entry in archive.infolist() if not entry.is_dir()]
        candidates = [entry for entry in files if entry.filename.endswith("/" + MANIFEST_PATH)]
        if len(candidates) != 1:
            raise InventoryError("the portable archive must contain exactly one inventory")
        prefix = candidates[0].filename.removesuffix(MANIFEST_PATH)
        raw = archive.read(candidates[0])
        if expected_inventory is not None and raw != expected_inventory:
            raise InventoryError("portable archive and sources name different inventories")
        found = {}
        for entry in files:
            relative_path(entry.filename)
            if not entry.filename.startswith(prefix):
                raise InventoryError("portable archive contains files outside the bundle")
            rel = entry.filename.removeprefix(prefix)
            if rel == MANIFEST_PATH:
                continue
            if rel in found:
                raise InventoryError(f"duplicate portable file: {rel}")
            with archive.open(entry) as stream:
                found[rel] = hashlib.file_digest(stream, "sha256").hexdigest()
        if found != json.loads(raw).get("payload"):
            raise InventoryError("portable file list or digest differs from inventory")
        return len(found)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--portable", type=Path)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--seal", metavar="EXE", help="seal a staged bundle with this executable name")
    parser.add_argument("--msys-root", type=Path)
    args = parser.parse_args(argv)
    if not args.bundle and not args.portable:
        parser.error("give --bundle or --portable")
    try:
        if args.portable:
            files = verify_portable(args.portable)
            print(f"{files} files verified in portable archive")
        if not args.bundle:
            return 0
        manifest = json.loads((args.bundle / MANIFEST_PATH).read_text(encoding="utf-8"))
        if args.seal:
            manifest = seal_bundle(args.bundle, manifest, executable=args.seal, msys_root=args.msys_root)
        verify_bundle(args.bundle, manifest, strict=args.strict)
    except (InventoryError, OSError, KeyError, BadZipFile) as exc:
        print(exc)
        return 1
    print(f"{len(manifest['files'])} records, {len(manifest['components'])} components, "
          f"{len(manifest['notices'])} original notices, {len(manifest['issues'])} issues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
