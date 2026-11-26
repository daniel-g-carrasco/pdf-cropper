#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Create, verify and audit versioned inventories for distributed artifacts."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import subprocess
import sys
import tempfile
from base64 import b64decode
from configparser import ConfigParser
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

SUFFIX = ".inventory.json"
SCHEMA = 1
ARTIFACT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.(?:exe|zip|flatpak|tar\.gz|whl)")
APP_ID = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+")
ARCH = re.compile(r"[A-Za-z0-9_-]+")
PAGES_REFS = "repo/refs/heads/app"
SCANNED = {"PyPI", "Go"}


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def component(ecosystem, name, version):
    if ecosystem == "PyPI":
        name = re.sub(r"[-_.]+", "-", name).lower()
    if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/+!~-]*", value)
               for value in (ecosystem, name, version)):
        raise ValueError("invalid component ecosystem, name or version")
    return ecosystem, name, version


def extra_components(values):
    result = []
    for value in values:
        kind, named = value.split(":", 1)
        name, version = named.rsplit("=", 1)
        result.append(component(kind, name, version))
    return result


def flatpak_components(root, runtime_commit=None):
    ini = ConfigParser(interpolation=None)
    with (root / "metadata").open(encoding="utf-8") as stream:
        ini.read_file(stream)
    runtime = ini["Application"]["runtime"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", runtime):
        raise ValueError("invalid Flatpak runtime reference")
    if runtime_commit is None:
        runtime_commit = subprocess.check_output(
            ["flatpak", "info", "--show-commit", "runtime/" + runtime], text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", runtime_commit):
        raise ValueError("invalid Flatpak runtime commit")
    roots = sorted((root / "files").glob("lib*/python*/site-packages"))
    wheels = [component("PyPI", dist.metadata["Name"], dist.version)
              for dist in importlib.metadata.distributions(path=roots)]
    return [*wheels, component("Flatpak", runtime, runtime_commit)]


def windows_components(path, *, gtk):
    data = json.loads(path.read_text(encoding="utf-8"))
    if gtk:
        if data["schema_version"] != 1:
            raise ValueError("unsupported Windows bundle inventory")
        ecosystems = {"wheel": "PyPI", "msys2": "MSYS2"}
        return [component(ecosystems[row["kind"]], row["name"], row["version"])
                for row in data["components"].values() if row["kind"] in ecosystems]
    return [component("PyPI", row["name"], row["version"]) for row in data]


def make_report(artifact, revision, components, platform):
    if not ARTIFACT.fullmatch(artifact.name):
        raise ValueError("invalid artifact name")
    return inventory_record(artifact.name, sha256(artifact), revision, components, platform)


def inventory_record(name, checksum, revision, components, platform):
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("invalid build revision")
    return {"schema_version": SCHEMA, "artifact": name, "sha256": checksum,
            "revision": revision, "platform": platform,
            "components": [dict(zip(("ecosystem", "name", "version"), row, strict=True))
                           for row in sorted(set(components))]}


def artifact_platform(name):
    if name.endswith((".flatpak", ".ostree-ref")):
        return "flatpak"
    if name.endswith(".tar.gz"):
        return "linux"
    if name.endswith(".whl"):
        return "wheel"
    return "windows"


def read_report(path, name, checksum, revision=None):
    with path.open(encoding="utf-8") as stream:
        report = json.loads(stream.read(2_000_000))
    return verify_report(report, name, checksum, revision)


def verify_report(report, name, checksum, revision=None):
    if report["schema_version"] != SCHEMA or report["artifact"] != name:
        raise ValueError(f"{name}: incorrect inventory schema or artifact name")
    if (not re.fullmatch(r"[a-f0-9]{64}", report["sha256"]) or report["sha256"] != checksum):
        raise ValueError(f"{name}: artifact SHA-256 differs from the inventory")
    sha = report["revision"]
    if not re.fullmatch(r"[a-f0-9]{40}", sha) or (revision is not None and sha != revision):
        raise ValueError(f"{name}: artifact revision differs from the expected commit")
    if report["platform"] != artifact_platform(name) or not isinstance(report["components"], list):
        raise ValueError(f"{name}: wrong platform or missing component list")
    for row in report["components"]:
        component(row["ecosystem"], row["name"], row["version"])
    return report


def check(artifacts, revision=None):
    return [read_report(path.with_name(path.name + SUFFIX), path.name, sha256(path), revision)
            for path in artifacts]


def pages_app_refs(pages, app_id):
    if not APP_ID.fullmatch(app_id):
        raise ValueError("invalid Flatpak application ID")
    return pages / PAGES_REFS / app_id


def pages_ref(pages, app_id, ref):
    if ref.is_symlink() or not ref.resolve().is_relative_to(pages.resolve()):
        raise ValueError("OSTree ref must stay inside the Pages checkout")
    name, commit, checksum = ostree_identity(app_id, ref.parent.name, ref.read_bytes())
    return name, pages / "inventories" / (name + SUFFIX), commit, checksum


def ostree_identity(app_id, arch, ref_bytes):
    if not ARCH.fullmatch(arch):
        raise ValueError("invalid OSTree architecture")
    if not re.fullmatch(rb"[a-f0-9]{64}\r?\n?", ref_bytes):
        raise ValueError("invalid OSTree commit ref")
    name = f"{app_id}-{arch}-stable.ostree-ref"
    # The OSTree text ref may be read from a Windows checkout with CRLF.
    commit = ref_bytes.decode("ascii").strip()
    checksum = hashlib.sha256(commit.encode("ascii")).hexdigest()
    return name, commit, checksum


def create_pages_report(pages, app_id, revision, components):
    runtime = next(name for ecosystem, name, _ in components if ecosystem == "Flatpak")
    arch = runtime.split("/")[1]
    ref = pages_app_refs(pages, app_id) / arch / "stable"
    name, path, commit, checksum = pages_ref(pages, app_id, ref)
    report = inventory_record(name, checksum, revision, components, "flatpak")
    report["ostree_commit"] = commit
    if not path.resolve().is_relative_to(pages.resolve()) or path.is_symlink():
        raise ValueError("OSTree inventory must stay inside the Pages checkout")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                     delete=False) as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    Path(stream.name).replace(path)
    print(path)


def pages_reports(pages, app_id):
    refs = sorted(pages_app_refs(pages, app_id).glob("*/stable"))
    if not refs:
        raise ValueError(f"{app_id}: no published OSTree stable refs")
    reports = []
    for ref in refs:
        name, path, commit, checksum = pages_ref(pages, app_id, ref)
        if path.is_symlink() or not path.resolve().is_relative_to(pages.resolve()):
            raise ValueError("OSTree inventory must stay inside the Pages checkout")
        report = read_report(path, name, checksum)
        if report["ostree_commit"] != commit:
            raise ValueError(f"{name}: published OSTree commit differs from the inventory")
        reports.append(report)
    print(f"Verified {len(reports)} inventories against published OSTree refs.")
    return reports


def repository_path(repo):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or any(
            part in {".", ".."} for part in repo.split("/")):
        raise ValueError("invalid repository name")
    return f"repos/{repo}"


def github_json(endpoint, *, optional=False):
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode:
        if optional and "(HTTP 404)" in result.stderr:
            return None
        raise ValueError(f"cannot read GitHub resource {endpoint}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def contents_blob(entry, *, limit):
    if entry.get("type") != "file" or entry.get("encoding") != "base64":
        raise ValueError("expected a base64 GitHub Contents file")
    raw = b64decode("".join(entry["content"].split()), validate=True)
    if len(raw) > limit:
        raise ValueError("GitHub Contents file exceeded the expected size")
    return raw


def github_pages_reports(repo, app_id=None):
    path = repository_path(repo)
    ref = github_json(f"{path}/git/ref/heads/gh-pages", optional=True)
    if ref is None:
        github_json(path)
        print("No gh-pages branch published yet.")
        return []
    revision = ref["object"]["sha"]
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("invalid Pages commit")

    def contents(relative, *, optional=False):
        return github_json(f"{path}/contents/{relative}?ref={revision}", optional=optional)

    listing = contents(PAGES_REFS, optional=True)
    if listing is None:
        if app_id:
            raise ValueError(f"{app_id}: no published OSTree stable refs")
        print("No OSTree app refs published on Pages yet.")
        return []
    apps = [app_id] if app_id else [entry["name"] for entry in listing if entry["type"] == "dir"]
    reports = []
    for app in sorted(apps):
        if not APP_ID.fullmatch(app):
            raise ValueError("invalid Flatpak app ID in Pages")
        app_path = f"{PAGES_REFS}/{app}"
        for entry in contents(app_path):
            if entry["type"] != "dir":
                continue
            arch = entry["name"]
            if not ARCH.fullmatch(arch):
                raise ValueError("invalid OSTree architecture in Pages")
            stable = contents(f"{app_path}/{arch}/stable", optional=True)
            if stable is None:
                continue
            name, commit, checksum = ostree_identity(app, arch, contents_blob(stable, limit=128))
            raw = contents_blob(contents(f"inventories/{name}{SUFFIX}"), limit=500_000)
            report = verify_report(json.loads(raw), name, checksum)
            if report["ostree_commit"] != commit:
                raise ValueError(f"{name}: published OSTree commit differs from the inventory")
            reports.append(report)
    if not reports:
        raise ValueError("no stable OSTree inventories could be verified")
    print(f"Verified {len(reports)} inventories against OSTree refs at Pages commit {revision}.")
    return reports


def osv(payload):
    request = Request("https://api.osv.dev/v1/querybatch", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read(8_000_000))


def advisories(components, query=osv):
    covered = sorted({row for row in components if row[0] in SCANNED})
    findings = set()
    for start in range(0, len(covered), 100):
        pending = [(row, {}) for row in covered[start:start + 100]]
        seen = set()
        while pending:
            queries = [{"package": {"ecosystem": kind, "name": name}, "version": version, **page}
                       for (kind, name, version), page in pending]
            results = query({"queries": queries})["results"]
            if not isinstance(results, list) or len(results) != len(pending):
                raise ValueError("OSV returned the wrong number of results")
            next_page = []
            for (row, _), result in zip(pending, results, strict=True):
                for item in result.get("vulns", []):
                    advisory = item["id"]
                    if not re.fullmatch(r"[A-Za-z0-9-]+", advisory):
                        raise ValueError("OSV returned an invalid advisory identifier")
                    findings.add((*row, advisory))
                token = result.get("next_page_token")
                if token:
                    if not isinstance(token, str) or (row, token) in seen:
                        raise ValueError("OSV returned an invalid pagination token")
                    seen.add((row, token))
                    next_page.append((row, {"page_token": token}))
            pending = next_page
    return sorted(findings)


def publishable_artifact(name):
    if not ARTIFACT.fullmatch(name):
        return False
    lowered = name.lower()
    if name.endswith(".tar.gz"):
        return "linux" in lowered and "portable" in lowered
    return not name.endswith(".zip") or ("source" not in lowered and
                                         any(part in lowered for part in ("portable", "windows")))


def release_reports(repo, tag):
    path = repository_path(repo)
    endpoint = "tags/" + quote(tag, safe="") if tag else "latest"
    release = github_json(f"{path}/releases/{endpoint}", optional=not tag)
    if release is None:
        github_json(path)
        print("No stable release published yet.")
        return []
    if release["draft"] or release["prerelease"]:
        raise ValueError("artifact auditing expects a published stable release")
    assets = {item["name"]: item for item in release["assets"] if item["state"] == "uploaded"}
    names = sorted(name for name in assets if publishable_artifact(name))
    if not names:
        raise ValueError("the stable release has no recognized distribution artifacts")
    with tempfile.TemporaryDirectory(prefix="release-inventory-") as temp:
        reports = []
        for name in names:
            sidecar = name + SUFFIX
            digest = assets[name].get("digest") or ""
            if not digest.startswith("sha256:") or sidecar not in assets:
                raise ValueError(f"{name}: published digest or inventory is missing")
            subprocess.run(["gh", "release", "download", "--repo", repo, "--pattern", sidecar,
                            "--dir", temp, "--", release["tag_name"]], check=True)
            path = Path(temp) / sidecar
            if assets[sidecar].get("digest") != "sha256:" + sha256(path):
                raise ValueError(f"{name}: downloaded inventory differs from the GitHub digest")
            reports.append(read_report(path, name, digest.removeprefix("sha256:")))
        print(f"Verified {len(reports)} inventories against GitHub digests for {release['tag_name']}.")
        return reports


def report_advisories(reports):
    components = {component(row["ecosystem"], row["name"], row["version"])
                  for report in reports for row in report["components"]}
    unscanned = sorted({kind for kind, _, _ in components} - SCANNED)
    if unscanned:
        print("Inventoried without OSV coverage: " + ", ".join(unscanned))
    count = sum(kind in SCANNED for kind, _, _ in components)
    print(f"Components eligible for OSV lookup: {count}.")
    findings = advisories(components)
    for kind, name, version, advisory in findings:
        print(f"{kind} {name} {version}: https://osv.dev/vulnerability/{advisory}")
    return int(bool(findings))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--revision", required=True)
    sources = create.add_mutually_exclusive_group()
    for source in ("gtk-inventory", "qt-inventory", "linux-inventory", "wheel-inventory", "flatpak"):
        sources.add_argument("--" + source, type=Path)
    verify = commands.add_parser("check")
    verify.add_argument("--revision")
    audit = commands.add_parser("audit")
    release = commands.add_parser("release")
    release.add_argument("--repo", required=True)
    release.add_argument("--tag")
    pages_create = commands.add_parser("pages-create")
    pages_create.add_argument("--revision", required=True)
    pages_create.add_argument("--flatpak", type=Path, required=True)
    for mode in (create, pages_create):
        mode.add_argument("--component", action="append", default=[], metavar="ECOSYSTEM:NAME=VERSION")
    pages_audit = commands.add_parser("pages-audit")
    pages_create.add_argument("--pages", type=Path, required=True)
    pages_create.add_argument("--app-id", required=True)
    pages_audit.add_argument("--app-id")
    location = pages_audit.add_mutually_exclusive_group(required=True)
    location.add_argument("--pages", type=Path)
    location.add_argument("--repo")
    for mode in (create, verify, audit):
        mode.add_argument("artifacts", type=Path, nargs="+")
    args = parser.parse_args(argv)
    try:
        if args.command == "release":
            return report_advisories(release_reports(args.repo, args.tag))
        if args.command == "pages-create":
            components = flatpak_components(args.flatpak) + extra_components(args.component)
            create_pages_report(args.pages, args.app_id, args.revision, components)
            return 0
        if args.command == "pages-audit":
            if args.repo:
                return report_advisories(github_pages_reports(args.repo, args.app_id))
            apps = [args.app_id] if args.app_id else sorted(
                path.name for path in (args.pages / PAGES_REFS).iterdir() if path.is_dir())
            if not apps:
                raise ValueError("no OSTree apps found in Pages")
            return report_advisories([report for app in apps for report in pages_reports(args.pages, app)])
        if args.command in ("check", "audit"):
            reports = check(args.artifacts, getattr(args, "revision", None))
            print(f"Verified {len(reports)} inventories against the local artifacts.")
            return report_advisories(reports) if args.command == "audit" else 0
        components = extra_components(args.component)
        if args.flatpak:
            components.extend(flatpak_components(args.flatpak))
        if path := args.gtk_inventory or args.qt_inventory or args.linux_inventory or args.wheel_inventory:
            components.extend(windows_components(path, gtk=args.gtk_inventory is not None))
        if not components:
            raise ValueError("pass a platform inventory or at least one installed component")
        supplied_platform = "windows"
        if args.flatpak:
            supplied_platform = "flatpak"
        elif args.linux_inventory:
            supplied_platform = "linux"
        elif args.wheel_inventory:
            supplied_platform = "wheel"
        for artifact in args.artifacts:
            platform = artifact_platform(artifact.name)
            if platform != supplied_platform:
                raise ValueError(f"{artifact.name}: artifact does not match the inventory platform")
            report = make_report(artifact, args.revision, components, platform)
            path = artifact.with_name(artifact.name + SUFFIX)
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(report, stream, indent=2)
                stream.write("\n")
            print(path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.CalledProcessError) as error:
        print(f"Inventory check incomplete: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
