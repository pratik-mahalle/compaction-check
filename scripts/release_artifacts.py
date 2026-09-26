"""Validate alpha distributions against the source/tag and write their checksums."""

import argparse
import ast
import hashlib
import re
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path


def validate(root, tag, directory):
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    version = project["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+a\d+", version) or tag != f"v{version}":
        raise ValueError("The release tag must match an alpha version in pyproject.toml.")
    tree = ast.parse((root / "src/compaction_check/__init__.py").read_text())
    versions = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
    ]
    if versions != [version]:
        raise ValueError("Package and distribution versions differ.")
    if not (root / f"docs/releases/{tag}.md").is_file():
        raise ValueError("Versioned release notes are missing.")
    wheel = directory / f"compaction_check-{version}-py3-none-any.whl"
    source = directory / f"compaction_check-{version}.tar.gz"
    prefix = f"compaction_check-{version}"
    license_text = (root / "LICENSE").read_bytes()
    with zipfile.ZipFile(wheel) as package, tarfile.open(source, "r:gz") as archive:
        metadata = BytesParser().parsebytes(package.read(f"{prefix}.dist-info/METADATA"))
        if (metadata["Name"], metadata["Version"], metadata["License-Expression"]) != (
            "compaction-check",
            version,
            "MIT",
        ):
            raise ValueError("Built distribution metadata does not match the release.")
        if package.read(f"{prefix}.dist-info/licenses/LICENSE") != license_text:
            raise ValueError("The wheel is missing the expected license.")
        source_metadata = archive.extractfile(f"{prefix}/PKG-INFO")
        if (
            source_metadata is None
            or BytesParser().parsebytes(source_metadata.read())["Version"] != version
        ):
            raise ValueError("Source archive version does not match the release.")
        for path in (root / "src/compaction_check").glob("*.py"):
            relative = f"compaction_check/{path.name}"
            saved = archive.extractfile(f"{prefix}/src/{relative}")
            if (
                saved is None
                or saved.read() != path.read_bytes()
                or package.read(relative) != path.read_bytes()
            ):
                raise ValueError(f"An artifact differs from the source: {relative}.")
        for relative in ("LICENSE", f"docs/releases/{tag}.md"):
            saved = archive.extractfile(f"{prefix}/{relative}")
            if saved is None or saved.read() != (root / relative).read_bytes():
                raise ValueError(f"The source archive has an incorrect {relative}.")
    checksums = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in (wheel, source)
    ]
    (directory / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    return version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--directory", type=Path, default=Path("release-dist"))
    args = parser.parse_args()
    try:
        version = validate(Path(__file__).resolve().parents[1], args.tag, args.directory)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, tarfile.TarError) as exc:
        parser.error(str(exc))
    print(f"Validated {version}: source, wheel, license, notes, and SHA256SUMS.")


if __name__ == "__main__":
    main()
