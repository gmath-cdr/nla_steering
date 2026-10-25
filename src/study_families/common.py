"""Shared, standard-library helpers for immutable paper artifacts.

The model runs are stored as verified ``tar.gz`` archives. Keeping the exact
archives avoids rewriting thousands of result files while still allowing a
fresh checkout to validate and extract every registered file.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = ROOT / "artifacts" / "paper_runs" / "data"


class ArtifactError(RuntimeError):
    """Raised when a frozen artifact is missing, unsafe, or inconsistent."""


@dataclass(frozen=True)
class ArchiveSpec:
    """Expected identity and headline cardinality for one completed run."""

    relative_path: str
    sha256: str
    complete_schema: str
    model_key: str
    phase: str
    expected_records: int

    @property
    def path(self) -> Path:
        return DATA_ROOT / self.relative_path


def sha256_file(path: Path) -> str:
    """Hash a file in bounded blocks so large judge outputs stay memory-safe."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_lf_text(path: Path) -> str:
    """Hash UTF-8 text after canonical CRLF-to-LF normalization.

    Historical execution receipts were sealed on Linux. The repository now
    enforces LF through ``.gitattributes``; this helper also lets an existing
    Windows checkout verify those receipts before its files are rechecked out.
    """

    payload = path.read_bytes().replace(b"\r\n", b"\n")
    return sha256_bytes(payload)


def canonical_sha256(value: Any) -> str:
    """Hash a JSON value using the canonical format sealed by the campaigns."""

    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256_bytes(payload)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_member_name(name: str) -> PurePosixPath:
    """Reject absolute paths and parent traversal before reading or extracting."""

    # Tar member names are POSIX paths. Backslashes or a drive-like first
    # component can become absolute after extraction on Windows even though
    # PurePosixPath does not classify them as absolute.
    if "\\" in name:
        raise ArtifactError(f"unsafe archive member: {name}")
    normalized = PurePosixPath(name)
    drive_like = bool(normalized.parts and normalized.parts[0].endswith(":"))
    if normalized.is_absolute() or drive_like or ".." in normalized.parts:
        raise ArtifactError(f"unsafe archive member: {name}")
    if not normalized.parts:
        raise ArtifactError("archive contains an empty member name")
    return normalized


def _member_bytes(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    stream = archive.extractfile(member)
    if stream is None:
        raise ArtifactError(f"archive member is not a regular file: {member.name}")
    return stream.read()


def _member_digest(archive: tarfile.TarFile, member: tarfile.TarInfo) -> tuple[int, str]:
    """Hash the current streaming member without seeking within a gzip file."""

    stream = archive.extractfile(member)
    if stream is None:
        raise ArtifactError(f"archive member is not a regular file: {member.name}")
    digest = hashlib.sha256()
    size = 0
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        size += len(block)
        digest.update(block)
    return size, digest.hexdigest()


def verify_complete_archive(spec: ArchiveSpec) -> dict[str, Any]:
    """Verify the outer archive, inner COMPLETE manifest, and every listed file."""

    path = spec.path
    if not path.is_file():
        raise ArtifactError(f"missing run archive: {path.relative_to(ROOT)}")
    actual_archive_hash = sha256_file(path)
    if actual_archive_hash != spec.sha256:
        raise ArtifactError(
            f"archive hash mismatch for {path.name}: "
            f"expected {spec.sha256}, got {actual_archive_hash}"
        )

    # COMPLETE.json appears near the start of each archive. Read it in one
    # streaming pass, then verify all registered payloads in a second pass.
    # Random seeks in a gzip-compressed tar would repeatedly decompress the
    # archive and turn this check into quadratic work.
    complete_name: str | None = None
    complete: dict[str, Any] | None = None
    with tarfile.open(path, mode="r|gz") as archive:
        for member in archive:
            _safe_member_name(member.name)
            if member.issym() or member.islnk():
                raise ArtifactError(f"archive contains a link: {member.name}")
            if member.isfile() and member.name.endswith("/COMPLETE.json"):
                if complete_name is not None:
                    raise ArtifactError(f"multiple COMPLETE.json files in {path.name}")
                complete_name = member.name
                complete = json.loads(_member_bytes(archive, member))
                break
    if complete_name is None or complete is None:
        raise ArtifactError(f"COMPLETE.json is missing from {path.name}")
    root_name = complete_name.rsplit("/", 1)[0]

    if complete.get("schema") != spec.complete_schema:
        raise ArtifactError(f"unexpected COMPLETE schema in {path.name}")
    if complete.get("status") != "complete":
        raise ArtifactError(f"archive is not marked complete: {path.name}")
    if complete.get("model_key") != spec.model_key:
        raise ArtifactError(f"model identity mismatch in {path.name}")

    registered = complete.get("files")
    if isinstance(registered, dict):
        rows: Iterable[tuple[str, dict[str, Any]]] = registered.items()
    elif isinstance(registered, list):
        rows = ((str(row["path"]), row) for row in registered)
    else:
        raise ArtifactError(f"COMPLETE file registry is malformed: {path.name}")
    expected_members: dict[str, dict[str, Any]] = {}
    for relative, metadata in rows:
        normalized = _safe_member_name(relative).as_posix()
        member_name = f"{root_name}/{normalized}"
        if member_name in expected_members:
            raise ArtifactError(f"duplicate COMPLETE registry path: {relative}")
        if not isinstance(metadata, dict) or "bytes" not in metadata or "sha256" not in metadata:
            raise ArtifactError(f"incomplete registry metadata: {relative}")
        expected_members[member_name] = metadata

    seen_names: set[str] = set()
    verified_names: set[str] = set()
    complete_count = 0
    with tarfile.open(path, mode="r|gz") as archive:
        for member in archive:
            _safe_member_name(member.name)
            if member.issym() or member.islnk():
                raise ArtifactError(f"archive contains a link: {member.name}")
            if not member.isfile():
                continue
            if member.name in seen_names:
                raise ArtifactError(f"duplicate archive member: {member.name}")
            seen_names.add(member.name)
            if member.name == complete_name:
                complete_count += 1
            metadata = expected_members.get(member.name)
            if metadata is None:
                continue
            size, digest = _member_digest(archive, member)
            if size != int(metadata["bytes"]):
                raise ArtifactError(f"registered size mismatch: {member.name}")
            if digest != metadata["sha256"]:
                raise ArtifactError(f"registered hash mismatch: {member.name}")
            verified_names.add(member.name)
    if complete_count != 1:
        raise ArtifactError(f"expected one COMPLETE.json in {path.name}, found {complete_count}")
    missing_members = set(expected_members) - verified_names
    if missing_members:
        example = sorted(missing_members)[0]
        raise ArtifactError(f"registered member is missing: {example}")
    registered_count = len(verified_names)

    if spec.phase == "evaluation":
        actual_records = int(complete["counts"]["evaluation"]["records"])
    elif spec.phase == "heldout":
        actual_records = int(complete["counts"]["heldout_records"])
    else:
        raise ArtifactError(f"unknown archive phase selector: {spec.phase}")
    if actual_records != spec.expected_records:
        raise ArtifactError(
            f"record count mismatch in {path.name}: "
            f"expected {spec.expected_records}, got {actual_records}"
        )

    return {
        "archive": path.relative_to(ROOT).as_posix(),
        "archive_sha256": actual_archive_hash,
        "root": root_name,
        "model_key": spec.model_key,
        "records": actual_records,
        "registered_files": registered_count,
        "complete": complete,
    }


def extract_verified_archive(spec: ArchiveSpec, destination: Path) -> Path:
    """Verify and safely extract one archive into a new, empty directory."""

    summary = verify_complete_archive(spec)
    destination = destination.resolve()
    data_root = DATA_ROOT.resolve()
    if destination == ROOT.resolve() or destination == data_root or data_root in destination.parents:
        raise ArtifactError(f"refusing extraction into a source/artifact input path: {destination}")
    if destination.exists() and not destination.is_dir():
        raise ArtifactError(f"extraction destination is not a directory: {destination}")
    if destination.exists() and any(destination.iterdir()):
        raise ArtifactError(f"refusing to overwrite non-empty directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    with tarfile.open(spec.path, mode="r|gz") as archive:
        for member in archive:
            normalized = _safe_member_name(member.name)
            if member.issym() or member.islnk():
                raise ArtifactError(f"archive contains a link: {member.name}")
            target = destination.joinpath(*normalized.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ArtifactError(f"unsupported archive member type: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = archive.extractfile(member)
            if stream is None:
                raise ArtifactError(f"archive member is not a regular file: {member.name}")
            with target.open("wb") as output:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    output.write(block)

    return destination / summary["root"]
