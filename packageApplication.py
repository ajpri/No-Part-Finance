"""Create a complete portable No Part Finance application package."""

import argparse
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path


applicationVersion = "26.10.3"
archiveRootName = "No-Part-Finance"
includedFiles = (
    "app.py",
    "users.py",
    "docs.html",
    "openapi.json",
    "tests/__init__.py",
    "tests/test_release_2610.py",
    "tests/test_pending_next.py",
    "tests/test_startup_serving.py",
    "app.js",
    "database.py",
    "index.html",
    "styles.css",
    "requirements.txt",
    "startWindows.bat",
    "startLinux.sh",
    "packageWindows.bat",
    "packageLinux.sh",
    "packageApplication.py",
    "README.md",
    "LICENSE",
    "screenshots/dashboard.jpg",
    "screenshots/transactions.jpg",
    "screenshots/budget.jpg",
    "screenshots/simulator.jpg",
    "SPEC.md",
    "ROADMAP.md",
    "PROMPTS.md",
    "AGENTS.md",
    "tests/test_app.py",
    "tests/test_budget.py",
    "tests/test_database.py",
    "tests/test_hardening.py",
    "tests/test_money_operations.py",
    "tests/test_packaging.py",
    "tests/test_reporting.py",
    "tests/test_release_26093.py",
    "tests/test_release_260918.py",
    "tests/test_support.py",
)


class PackagingError(Exception):
    """A clear, user-facing package creation failure."""


def verifyDatabase(connection, checkForeignKeys=False):
    integrityRows = connection.execute("PRAGMA integrity_check").fetchall()
    if integrityRows != [("ok",)]:
        raise PackagingError("Database integrity check failed.")
    if checkForeignKeys and connection.execute("PRAGMA foreign_key_check").fetchall():
        raise PackagingError("Database foreign key check failed.")


def createVerifiedDatabaseCopy(sourceDatabasePath, destinationDatabasePath):
    sourceDatabasePath = Path(sourceDatabasePath).resolve()
    if not sourceDatabasePath.is_file():
        raise PackagingError(f"Source database is absent: {sourceDatabasePath}")
    try:
        sourceUri = sourceDatabasePath.as_uri() + "?mode=ro"
        sourceConnection = sqlite3.connect(sourceUri, uri=True)
        try:
            verifyDatabase(sourceConnection)
            destinationConnection = sqlite3.connect(destinationDatabasePath)
            try:
                sourceConnection.backup(destinationConnection)
            finally:
                destinationConnection.close()
        finally:
            sourceConnection.close()
        verifiedConnection = sqlite3.connect(destinationDatabasePath)
        try:
            verifyDatabase(verifiedConnection, checkForeignKeys=True)
        finally:
            verifiedConnection.close()
    except PackagingError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise PackagingError(f"Unable to read and back up the source database: {error}") from error


def resolveOutputDirectory(repositoryPath, outputPath=None):
    repositoryPath = Path(repositoryPath).resolve()
    destination = Path(outputPath) if outputPath is not None else Path("packages")
    if not destination.is_absolute():
        destination = repositoryPath / destination
    try:
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination, prefix=".package-write-test-", delete=True):
            pass
    except OSError as error:
        raise PackagingError(f"Package output directory is not writable: {destination}") from error
    return destination.resolve()


def uniqueArchivePath(outputDirectory, timestamp, includeDatabase=True):
    databaseLabel = "" if includeDatabase else "-no-db"
    baseName = f"No-Part-Finance-{applicationVersion}{databaseLabel}-{timestamp.strftime('%Y-%m-%d-%H%M%S')}"
    candidate = outputDirectory / f"{baseName}.zip"
    suffix = 2
    while candidate.exists():
        candidate = outputDirectory / f"{baseName}-{suffix}.zip"
        suffix += 1
    return candidate


def validateIncludedFiles(repositoryPath):
    missing = [relativePath for relativePath in includedFiles if not (repositoryPath / relativePath).is_file()]
    if missing:
        raise PackagingError(f"Required package file is absent: {missing[0]}")


def createApplicationPackage(repositoryPath=None, databasePath=None, outputPath=None, createdAt=None, includeDatabase=True):
    repositoryPath = Path(repositoryPath or Path(__file__).resolve().parent).resolve()
    databasePath = Path(databasePath or repositoryPath / "finance.db").resolve()
    createdAt = createdAt or datetime.now().astimezone()
    validateIncludedFiles(repositoryPath)
    outputDirectory = resolveOutputDirectory(repositoryPath, outputPath)
    archivePath = uniqueArchivePath(outputDirectory, createdAt, includeDatabase)

    with tempfile.TemporaryDirectory(prefix="no-part-finance-package-") as temporaryDirectory:
        verifiedDatabasePath = Path(temporaryDirectory) / "finance.db"
        temporaryArchivePath = Path(temporaryDirectory) / archivePath.name
        if includeDatabase:
            createVerifiedDatabaseCopy(databasePath, verifiedDatabasePath)
        finalArchiveCreated = False
        try:
            with zipfile.ZipFile(temporaryArchivePath, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for relativePath in includedFiles:
                    sourcePath = repositoryPath / relativePath
                    archiveName = f"{archiveRootName}/{relativePath}"
                    if relativePath.endswith(".sh"):
                        fileDetails = zipfile.ZipInfo.from_file(sourcePath, archiveName)
                        fileDetails.compress_type = zipfile.ZIP_DEFLATED
                        fileDetails.create_system = 3
                        fileDetails.external_attr = 0o100755 << 16
                        archive.writestr(fileDetails, sourcePath.read_bytes())
                    else:
                        archive.write(sourcePath, archiveName)
                if includeDatabase:
                    archive.write(verifiedDatabasePath, f"{archiveRootName}/finance.db")
            with temporaryArchivePath.open("rb") as sourceArchive, archivePath.open("xb") as finalArchive:
                finalArchiveCreated = True
                shutil.copyfileobj(sourceArchive, finalArchive)
        except (OSError, zipfile.BadZipFile) as error:
            if finalArchiveCreated:
                archivePath.unlink(missing_ok=True)
            raise PackagingError(f"Unable to create application package: {error}") from error

    return {
        "archivePath": archivePath,
        "version": applicationVersion,
        "createdAt": createdAt,
        "databasePath": databasePath,
        "includesDatabase": includeDatabase,
        "sizeBytes": archivePath.stat().st_size,
    }


def argumentParser():
    parser = argparse.ArgumentParser(
        description="Create one complete portable No Part Finance snapshot under a No-Part-Finance folder."
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        help="destination directory (relative paths resolve from the repository root; default: packages)",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="create an application-only ZIP without finance.db",
    )
    parser.epilog = (
        "The ZIP includes maintained application files, documentation, tests, launchers, and a verified "
        "finance.db by default. WARNING: the default package contains your complete unencrypted financial database and recoverable credentials. "
        "Use --no-db for an application-only package. Examples: packageWindows.bat --no-db "
        "--output D:\\Packages | ./packageLinux.sh --no-db --output /srv/packages"
    )
    return parser


def main(arguments=None):
    options = argumentParser().parse_args(arguments)
    try:
        result = createApplicationPackage(outputPath=options.output, includeDatabase=not options.no_db)
    except PackagingError as error:
        raise SystemExit(f"Packaging failed: {error}") from error
    timestamp = result["createdAt"].strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"Archive: {result['archivePath']}")
    print(f"Version: {result['version']}")
    print(f"Local timestamp: {timestamp}")
    print(f"Source database: {result['databasePath']}" if result["includesDatabase"] else "Source database: not included")
    print(f"Archive size: {result['sizeBytes']} bytes")
    if result["includesDatabase"]:
        print("WARNING: This ZIP contains the complete unencrypted financial database and recoverable credentials.")
        print("Browser download (sign in as Owner first): http://HOST:PORT/api/package")
    else:
        print("This ZIP does not contain finance.db.")
        print("Browser download (sign in as Owner or Auditor first): http://HOST:PORT/api/package-no-db")


if __name__ == "__main__":
    main()
