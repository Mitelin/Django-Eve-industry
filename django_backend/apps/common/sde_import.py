from __future__ import annotations

from collections.abc import Iterator
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Any
from urllib.parse import urlparse
import zipfile

import httpx
from django.core.files.uploadedfile import UploadedFile
from django.db import connections, transaction
from django.utils import timezone

from apps.common.models import SdeImportRun, SdeImportState


REQUIRED_SDE_FILES = {
    "_sde.jsonl",
    "categories.jsonl",
    "groups.jsonl",
    "types.jsonl",
    "blueprints.jsonl",
    "typeMaterials.jsonl",
}
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_STATE_SOURCE = "ccp_sde"


class SdeImportError(RuntimeError):
    pass


class SdeArchiveValidationError(SdeImportError):
    pass


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            value = line.strip()
            if not value:
                continue
            payload = json.loads(value)
            if isinstance(payload, dict):
                yield payload


def _extract_name_en(value: Any) -> str:
    if isinstance(value, dict):
        english = value.get("en")
        if isinstance(english, str) and english.strip():
            return english
        for candidate in value.values():
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        return ""
    return str(value or "")


def _read_build_info(extract_dir: Path) -> dict[str, Any]:
    version_path = extract_dir / "_sde.jsonl"
    first = next(_iter_jsonl(version_path), None)
    if not isinstance(first, dict):
        raise SdeArchiveValidationError("_sde.jsonl is missing a valid metadata row")
    build_number = first.get("buildNumber")
    if build_number is None:
        raise SdeArchiveValidationError("_sde.jsonl is missing buildNumber")
    return {
        "buildNumber": int(build_number),
        "releaseDate": str(first.get("releaseDate") or ""),
    }


def _serialize_run(run: SdeImportRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "status": run.status,
        "sourceType": run.source_type,
        "sourceUrl": run.source_url,
        "sourceFilename": run.source_filename,
        "archiveSha256": run.archive_sha256,
        "triggeredBy": run.triggered_by,
        "detectedBuildNumber": run.detected_build_number,
        "detectedReleaseDate": run.detected_release_date,
        "previousBuildNumber": run.previous_build_number,
        "importedBuildNumber": run.imported_build_number,
        "forceReimport": run.force_reimport,
        "tableCounts": run.table_counts,
        "notes": run.notes,
        "errorText": run.error_text,
        "createdAt": run.created_at.isoformat().replace("+00:00", "Z"),
        "updatedAt": run.updated_at.isoformat().replace("+00:00", "Z"),
    }


def _serialize_state(state: SdeImportState | None) -> dict[str, Any]:
    if state is None:
        return {
            "source": DEFAULT_STATE_SOURCE,
            "currentBuildNumber": None,
            "currentReleaseDate": "",
            "archiveSha256": "",
            "archiveSourceUrl": "",
            "sourceFilename": "",
            "lastCheckedAt": None,
            "lastImportedAt": None,
        }
    return {
        "source": state.source,
        "currentBuildNumber": state.current_build_number,
        "currentReleaseDate": state.current_release_date,
        "archiveSha256": state.archive_sha256,
        "archiveSourceUrl": state.archive_source_url,
        "sourceFilename": state.source_filename,
        "lastCheckedAt": state.last_checked_at.isoformat().replace("+00:00", "Z") if state.last_checked_at else None,
        "lastImportedAt": state.last_imported_at.isoformat().replace("+00:00", "Z") if state.last_imported_at else None,
    }


def get_sde_import_summary(*, limit: int = 10) -> dict[str, Any]:
    state = SdeImportState.objects.filter(source=DEFAULT_STATE_SOURCE).first()
    runs = [_serialize_run(run) for run in SdeImportRun.objects.all()[:limit]]
    return {
        "state": _serialize_state(state),
        "runs": runs,
    }


def download_sde_archive(*, archive_url: str, timeout_seconds: float = 180.0) -> dict[str, Any]:
    parsed = urlparse(archive_url)
    if parsed.scheme not in {"http", "https"}:
        raise SdeArchiveValidationError("archiveUrl must use http or https")
    filename = Path(parsed.path or "ccp_sde.zip").name or "ccp_sde.zip"
    if not filename.lower().endswith(".zip"):
        raise SdeArchiveValidationError("archiveUrl must point to a .zip archive")

    file_handle = tempfile.NamedTemporaryFile(prefix="ccp_sde_", suffix=".zip", delete=False)
    archive_path = Path(file_handle.name)
    digest = hashlib.sha256()
    total_bytes = 0

    try:
        with httpx.stream("GET", archive_url, follow_redirects=True, timeout=timeout_seconds) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > MAX_ARCHIVE_BYTES:
                raise SdeArchiveValidationError("archive is too large")
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                total_bytes += len(chunk)
                if total_bytes > MAX_ARCHIVE_BYTES:
                    raise SdeArchiveValidationError("archive exceeded maximum allowed size")
                digest.update(chunk)
                file_handle.write(chunk)
    except Exception:
        file_handle.close()
        archive_path.unlink(missing_ok=True)
        raise

    file_handle.close()
    return {
        "archivePath": archive_path,
        "sourceFilename": filename,
        "archiveSha256": digest.hexdigest(),
        "sizeBytes": total_bytes,
    }


def save_uploaded_sde_archive(*, uploaded_file: UploadedFile) -> dict[str, Any]:
    filename = str(uploaded_file.name or "ccp_sde.zip")
    if not filename.lower().endswith(".zip"):
        raise SdeArchiveValidationError("uploaded archive must be a .zip file")

    file_handle = tempfile.NamedTemporaryFile(prefix="ccp_sde_upload_", suffix=".zip", delete=False)
    archive_path = Path(file_handle.name)
    digest = hashlib.sha256()
    total_bytes = 0

    try:
        for chunk in uploaded_file.chunks():
            if not chunk:
                continue
            total_bytes += len(chunk)
            if total_bytes > MAX_ARCHIVE_BYTES:
                raise SdeArchiveValidationError("uploaded archive exceeded maximum allowed size")
            digest.update(chunk)
            file_handle.write(chunk)
    except Exception:
        file_handle.close()
        archive_path.unlink(missing_ok=True)
        raise

    file_handle.close()
    return {
        "archivePath": archive_path,
        "sourceFilename": filename,
        "archiveSha256": digest.hexdigest(),
        "sizeBytes": total_bytes,
    }


def _extract_required_files(*, archive_path: Path, extract_dir: Path) -> list[str]:
    extracted: dict[str, zipfile.ZipInfo] = {}
    try:
        with zipfile.ZipFile(archive_path) as archive:
            total_uncompressed = sum(info.file_size for info in archive.infolist())
            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise SdeArchiveValidationError("archive expands beyond maximum allowed size")

            for info in archive.infolist():
                if info.is_dir():
                    continue
                basename = PurePosixPath(info.filename).name
                if basename not in REQUIRED_SDE_FILES:
                    continue
                if basename in extracted:
                    raise SdeArchiveValidationError(f"archive contains duplicate required file: {basename}")
                extracted[basename] = info

            missing = sorted(REQUIRED_SDE_FILES - set(extracted))
            if missing:
                raise SdeArchiveValidationError(f"archive is missing required files: {', '.join(missing)}")

            for basename, info in extracted.items():
                target_path = extract_dir / basename
                with archive.open(info) as source, target_path.open("wb") as target:
                    shutil.copyfileobj(source, target)
    except zipfile.BadZipFile as exc:
        raise SdeArchiveValidationError("archive is not a valid ZIP file") from exc

    return sorted(extracted)


def _ensure_minimal_sde_schema(connection_alias: str = "default") -> None:
    connection = connections[connection_alias]
    statements = [
        "CREATE TABLE IF NOT EXISTS invCategories (categoryID INTEGER NOT NULL PRIMARY KEY, categoryName TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS invGroups (groupID INTEGER NOT NULL PRIMARY KEY, groupName TEXT NOT NULL, categoryID INTEGER NOT NULL)",
        "CREATE TABLE IF NOT EXISTS invTypes (typeID INTEGER NOT NULL PRIMARY KEY, typeName TEXT NOT NULL, groupID INTEGER NOT NULL, portionSize INTEGER NOT NULL DEFAULT 1)",
        "CREATE TABLE IF NOT EXISTS invMetaTypes (typeID INTEGER NOT NULL PRIMARY KEY, metaGroupID INTEGER NOT NULL)",
        "CREATE TABLE IF NOT EXISTS industryBlueprints (typeID INTEGER NOT NULL PRIMARY KEY, maxProductionLimit INTEGER NOT NULL)",
        "CREATE TABLE IF NOT EXISTS industryActivity (typeID INTEGER NOT NULL, activityID INTEGER NOT NULL, time INTEGER NOT NULL, PRIMARY KEY (typeID, activityID))",
        "CREATE TABLE IF NOT EXISTS industryActivityProducts (typeID INTEGER NOT NULL, activityID INTEGER NOT NULL, productTypeID INTEGER NOT NULL, quantity INTEGER NOT NULL, PRIMARY KEY (typeID, activityID, productTypeID))",
        "CREATE TABLE IF NOT EXISTS industryActivityMaterials (typeID INTEGER NOT NULL, activityID INTEGER NOT NULL, materialTypeID INTEGER NOT NULL, quantity INTEGER NOT NULL, PRIMARY KEY (typeID, activityID, materialTypeID))",
        "CREATE TABLE IF NOT EXISTS industryActivityProbabilities (typeID INTEGER NOT NULL, activityID INTEGER NOT NULL, productTypeID INTEGER NOT NULL, probability REAL NOT NULL, PRIMARY KEY (typeID, activityID, productTypeID))",
        "CREATE TABLE IF NOT EXISTS invTypeMaterials (typeID INTEGER NOT NULL, materialTypeID INTEGER NOT NULL, quantity INTEGER NOT NULL, PRIMARY KEY (typeID, materialTypeID))",
        "CREATE TABLE IF NOT EXISTS sdeVersion (source TEXT NOT NULL PRIMARY KEY, buildNumber BIGINT NOT NULL, releaseDate TEXT NULL, importedAt TIMESTAMP NOT NULL)",
        "CREATE INDEX IF NOT EXISTS idx_invGroups_categoryID ON invGroups (categoryID)",
        "CREATE INDEX IF NOT EXISTS idx_invTypes_groupID ON invTypes (groupID)",
        "CREATE INDEX IF NOT EXISTS idx_indActProd_productTypeID ON industryActivityProducts (productTypeID)",
        "CREATE INDEX IF NOT EXISTS idx_indActMat_materialTypeID ON industryActivityMaterials (materialTypeID)",
        "CREATE INDEX IF NOT EXISTS idx_invTypeMaterials_materialTypeID ON invTypeMaterials (materialTypeID)",
    ]
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)


def _replace_table_data(*, extract_dir: Path, build_info: dict[str, Any], connection_alias: str = "default") -> dict[str, int]:
    _ensure_minimal_sde_schema(connection_alias)
    connection = connections[connection_alias]
    counts = {
        "invCategories": 0,
        "invGroups": 0,
        "invTypes": 0,
        "invMetaTypes": 0,
        "industryBlueprints": 0,
        "industryActivity": 0,
        "industryActivityProducts": 0,
        "industryActivityMaterials": 0,
        "industryActivityProbabilities": 0,
        "invTypeMaterials": 0,
        "sdeVersion": 1,
    }
    delete_order = [
        "industryActivityProbabilities",
        "industryActivityMaterials",
        "industryActivityProducts",
        "industryActivity",
        "industryBlueprints",
        "invMetaTypes",
        "invTypeMaterials",
        "invTypes",
        "invGroups",
        "invCategories",
        "sdeVersion",
    ]

    with transaction.atomic(using=connection_alias):
        with connection.cursor() as cursor:
            for table_name in delete_order:
                cursor.execute(f"DELETE FROM {table_name}")

            category_rows: list[tuple[int, str]] = []
            for obj in _iter_jsonl(extract_dir / "categories.jsonl"):
                category_rows.append((int(obj.get("_key")), _extract_name_en(obj.get("name"))))
            cursor.executemany("INSERT INTO invCategories (categoryID, categoryName) VALUES (%s, %s)", category_rows)
            counts["invCategories"] = len(category_rows)

            group_rows: list[tuple[int, str, int]] = []
            for obj in _iter_jsonl(extract_dir / "groups.jsonl"):
                group_rows.append((int(obj.get("_key")), _extract_name_en(obj.get("name")), int(obj.get("categoryID") or 0)))
            cursor.executemany("INSERT INTO invGroups (groupID, groupName, categoryID) VALUES (%s, %s, %s)", group_rows)
            counts["invGroups"] = len(group_rows)

            type_rows: list[tuple[int, str, int, int]] = []
            meta_rows: list[tuple[int, int]] = []
            for obj in _iter_jsonl(extract_dir / "types.jsonl"):
                type_id = int(obj.get("_key"))
                type_rows.append((type_id, _extract_name_en(obj.get("name")), int(obj.get("groupID") or 0), int(obj.get("portionSize") or 1)))
                if obj.get("metaGroupID") is not None:
                    meta_rows.append((type_id, int(obj.get("metaGroupID"))))
            cursor.executemany(
                "INSERT INTO invTypes (typeID, typeName, groupID, portionSize) VALUES (%s, %s, %s, %s)",
                type_rows,
            )
            counts["invTypes"] = len(type_rows)
            if meta_rows:
                cursor.executemany("INSERT INTO invMetaTypes (typeID, metaGroupID) VALUES (%s, %s)", meta_rows)
            counts["invMetaTypes"] = len(meta_rows)

            blueprint_rows: list[tuple[int, int]] = []
            activity_rows: list[tuple[int, int, int]] = []
            product_rows: list[tuple[int, int, int, int]] = []
            material_rows: list[tuple[int, int, int, int]] = []
            probability_rows: list[tuple[int, int, int, float]] = []
            activity_name_to_id = {
                "manufacturing": 1,
                "research_time": 3,
                "research_material": 4,
                "copying": 5,
                "invention": 8,
                "reaction": 11,
                "reactions": 11,
            }
            for obj in _iter_jsonl(extract_dir / "blueprints.jsonl"):
                type_id = int(obj.get("blueprintTypeID") or obj.get("_key"))
                blueprint_rows.append((type_id, int(obj.get("maxProductionLimit") or 0)))
                activities = obj.get("activities")
                if not isinstance(activities, dict):
                    continue
                for activity_name, activity in activities.items():
                    activity_id = activity_name_to_id.get(str(activity_name))
                    if activity_id is None or not isinstance(activity, dict):
                        continue
                    activity_rows.append((type_id, activity_id, int(activity.get("time") or 0)))
                    for material in activity.get("materials") or []:
                        if not isinstance(material, dict) or material.get("typeID") is None:
                            continue
                        material_rows.append((type_id, activity_id, int(material.get("typeID")), int(material.get("quantity") or 0)))
                    for product in activity.get("products") or []:
                        if not isinstance(product, dict) or product.get("typeID") is None:
                            continue
                        product_type_id = int(product.get("typeID"))
                        product_rows.append((type_id, activity_id, product_type_id, int(product.get("quantity") or 1)))
                        if product.get("probability") is not None:
                            probability_rows.append((type_id, activity_id, product_type_id, float(product.get("probability"))))

            cursor.executemany(
                "INSERT INTO industryBlueprints (typeID, maxProductionLimit) VALUES (%s, %s)",
                blueprint_rows,
            )
            counts["industryBlueprints"] = len(blueprint_rows)
            if activity_rows:
                cursor.executemany(
                    "INSERT INTO industryActivity (typeID, activityID, time) VALUES (%s, %s, %s)",
                    activity_rows,
                )
            counts["industryActivity"] = len(activity_rows)
            if product_rows:
                cursor.executemany(
                    "INSERT INTO industryActivityProducts (typeID, activityID, productTypeID, quantity) VALUES (%s, %s, %s, %s)",
                    product_rows,
                )
            counts["industryActivityProducts"] = len(product_rows)
            if material_rows:
                cursor.executemany(
                    "INSERT INTO industryActivityMaterials (typeID, activityID, materialTypeID, quantity) VALUES (%s, %s, %s, %s)",
                    material_rows,
                )
            counts["industryActivityMaterials"] = len(material_rows)
            if probability_rows:
                cursor.executemany(
                    "INSERT INTO industryActivityProbabilities (typeID, activityID, productTypeID, probability) VALUES (%s, %s, %s, %s)",
                    probability_rows,
                )
            counts["industryActivityProbabilities"] = len(probability_rows)

            type_material_rows: list[tuple[int, int, int]] = []
            for obj in _iter_jsonl(extract_dir / "typeMaterials.jsonl"):
                type_id = int(obj.get("_key"))
                for material in obj.get("materials") or []:
                    if not isinstance(material, dict) or material.get("materialTypeID") is None:
                        continue
                    type_material_rows.append((type_id, int(material.get("materialTypeID")), int(material.get("quantity") or 0)))
            if type_material_rows:
                cursor.executemany(
                    "INSERT INTO invTypeMaterials (typeID, materialTypeID, quantity) VALUES (%s, %s, %s)",
                    type_material_rows,
                )
            counts["invTypeMaterials"] = len(type_material_rows)

            cursor.execute(
                "INSERT INTO sdeVersion (source, buildNumber, releaseDate, importedAt) VALUES (%s, %s, %s, %s)",
                [DEFAULT_STATE_SOURCE, int(build_info["buildNumber"]), str(build_info.get("releaseDate") or ""), timezone.now()],
            )

    return counts


def import_sde_archive(
    *,
    archive_path: str | Path,
    source_url: str = "",
    source_filename: str = "",
    archive_sha256: str = "",
    triggered_by: str = "",
    force_reimport: bool = False,
    connection_alias: str = "default",
) -> dict[str, Any]:
    archive_path = Path(archive_path)
    if not archive_path.exists():
        raise SdeArchiveValidationError(f"archive not found: {archive_path}")

    state, _created = SdeImportState.objects.get_or_create(source=DEFAULT_STATE_SOURCE)
    run = SdeImportRun.objects.create(
        status=SdeImportRun.Status.STARTED,
        source_type="url",
        source_url=source_url,
        source_filename=source_filename or archive_path.name,
        archive_sha256=archive_sha256,
        triggered_by=triggered_by,
        previous_build_number=state.current_build_number,
        force_reimport=force_reimport,
    )
    now = timezone.now()

    try:
        with tempfile.TemporaryDirectory(prefix="ccp_sde_extract_") as temp_dir:
            extract_dir = Path(temp_dir)
            extracted_files = _extract_required_files(archive_path=archive_path, extract_dir=extract_dir)
            build_info = _read_build_info(extract_dir)

            run.detected_build_number = int(build_info["buildNumber"])
            run.detected_release_date = str(build_info.get("releaseDate") or "")
            state.last_checked_at = now

            if (
                state.current_build_number is not None
                and int(build_info["buildNumber"]) <= int(state.current_build_number)
                and not force_reimport
            ):
                run.status = SdeImportRun.Status.SKIPPED
                run.notes = "Archive build is not newer than the currently imported SDE build."
                run.save(update_fields=[
                    "detected_build_number",
                    "detected_release_date",
                    "status",
                    "notes",
                    "updated_at",
                ])
                state.save(update_fields=["last_checked_at", "updated_at"])
                return {
                    "imported": False,
                    "skipped": True,
                    "extractedFiles": extracted_files,
                    "state": _serialize_state(state),
                    "run": _serialize_run(run),
                }

            table_counts = _replace_table_data(extract_dir=extract_dir, build_info=build_info, connection_alias=connection_alias)
            state.current_build_number = int(build_info["buildNumber"])
            state.current_release_date = str(build_info.get("releaseDate") or "")
            state.archive_sha256 = archive_sha256
            state.archive_source_url = source_url
            state.source_filename = source_filename or archive_path.name
            state.last_imported_at = now
            state.last_checked_at = now
            state.save()

            run.status = SdeImportRun.Status.SUCCEEDED
            run.imported_build_number = int(build_info["buildNumber"])
            run.table_counts = table_counts
            run.notes = "Full CCP SDE reimport completed successfully."
            run.save(update_fields=[
                "detected_build_number",
                "detected_release_date",
                "status",
                "imported_build_number",
                "table_counts",
                "notes",
                "updated_at",
            ])
            return {
                "imported": True,
                "skipped": False,
                "extractedFiles": extracted_files,
                "state": _serialize_state(state),
                "run": _serialize_run(run),
            }
    except SdeArchiveValidationError as exc:
        run.status = SdeImportRun.Status.VALIDATION_FAILED
        run.error_text = str(exc)
        run.save(update_fields=["status", "error_text", "updated_at"])
        state.last_checked_at = now
        state.save(update_fields=["last_checked_at", "updated_at"])
        raise
    except Exception as exc:
        run.status = SdeImportRun.Status.FAILED
        run.error_text = str(exc)
        run.save(update_fields=["status", "error_text", "updated_at"])
        state.last_checked_at = now
        state.save(update_fields=["last_checked_at", "updated_at"])
        raise SdeImportError(str(exc)) from exc


def import_sde_from_url(
    *,
    archive_url: str,
    triggered_by: str = "",
    force_reimport: bool = False,
    connection_alias: str = "default",
) -> dict[str, Any]:
    download_result = download_sde_archive(archive_url=archive_url)
    archive_path = Path(download_result["archivePath"])
    try:
        result = import_sde_archive(
            archive_path=archive_path,
            source_url=archive_url,
            source_filename=str(download_result.get("sourceFilename") or archive_path.name),
            archive_sha256=str(download_result.get("archiveSha256") or ""),
            triggered_by=triggered_by,
            force_reimport=force_reimport,
            connection_alias=connection_alias,
        )
        result["download"] = {
            "archiveSha256": str(download_result.get("archiveSha256") or ""),
            "sizeBytes": int(download_result.get("sizeBytes") or 0),
        }
        return result
    finally:
        archive_path.unlink(missing_ok=True)


def import_sde_from_upload(
    *,
    uploaded_file: UploadedFile,
    triggered_by: str = "",
    force_reimport: bool = False,
    connection_alias: str = "default",
) -> dict[str, Any]:
    upload_result = save_uploaded_sde_archive(uploaded_file=uploaded_file)
    archive_path = Path(upload_result["archivePath"])
    try:
        result = import_sde_archive(
            archive_path=archive_path,
            source_url="",
            source_filename=str(upload_result.get("sourceFilename") or archive_path.name),
            archive_sha256=str(upload_result.get("archiveSha256") or ""),
            triggered_by=triggered_by,
            force_reimport=force_reimport,
            connection_alias=connection_alias,
        )
        result["upload"] = {
            "archiveSha256": str(upload_result.get("archiveSha256") or ""),
            "sizeBytes": int(upload_result.get("sizeBytes") or 0),
        }
        return result
    finally:
        archive_path.unlink(missing_ok=True)