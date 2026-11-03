"""Unattributed."""

from __future__ import annotations

import re

from printstash_core.files import is_main_file
from sqlmodel import Session, col, select

from app.db.models import (
    File,
    FileType,
    Model,
)
from app.db.scopes import live

_SLICER_SUFFIX = re.compile(
    r"(_[a-z][a-z0-9-]*_(\d+[hms])+)?(\.gcode)?\.(3mf|stl)$", re.IGNORECASE
)


def upload_base(name: str) -> str:
    """A file name as Orca writes it into an upload: `+`, `,`, `%` become `_`
    and `_PLA_1h2m.gcode.3mf` is appended."""
    return re.sub(r"[^a-z0-9]+", "_", _SLICER_SUFFIX.sub("", name).lower())


def print_stem(name: str) -> str:
    """One name for a library file and its slicer upload, also across the
    `_AD5X` copy and a project saved per layer height (`_0.20mm`)."""
    name = upload_base(name).replace("_ad5x", "").strip("_")
    return re.sub(r"_\d+_\d+mm$", "", name)


def artifact_for_printer_filename(
    session: Session, filename: str
) -> tuple[int, int] | None:
    """Return (file_id, model_id) of the one live Artifact ``filename`` names.

    Printers report only the name a slicer uploaded, so this is a name match,
    not proof of identical bytes. A G-code file of exactly that name comes
    first, then the 3MF the upload was named after, then any 3MF of the same
    print stem, preferring a folder's main ``_AD5X`` file. Two candidates are
    left unlinked rather than guessed.
    """
    rows = session.exec(
        select(File.id, File.model_id, File.original_filename, File.file_type)
        .join(Model, col(Model.id) == File.model_id)
        .where(
            col(File.file_type).in_([FileType.GCODE, FileType.THREE_MF]),
            live(File),
            live(Model),
        )
    ).all()
    projects = [r for r in rows if r[3] == FileType.THREE_MF]
    found = [r for r in rows if r[3] == FileType.GCODE and r[2] == filename]
    if not found:
        base = upload_base(filename)
        found = [r for r in projects if upload_base(r[2]) == base]
    if not found:
        stem = print_stem(filename)
        found = [r for r in projects if print_stem(r[2]) == stem]
        found = [r for r in found if is_main_file(r[2])] or found
    if len(found) != 1 or found[0][0] is None:
        return None
    return int(found[0][0]), int(found[0][1])


def ensure_unattributed_artifact(session: Session) -> tuple[int, int]:
    """Return (file_id, model_id) of lazily-created external job sentinel rows."""
    from app.db.models import (
        SENTINEL_FILE_HASH,
        SENTINEL_MODEL_HASH,
        File,
        Model,
    )

    model = session.exec(select(Model).where(Model.hash == SENTINEL_MODEL_HASH)).first()
    if model is None:
        model = Model(
            name="__external__",
            slug="__external__",
            hash=SENTINEL_MODEL_HASH,
        )
        session.add(model)
        session.commit()
        session.refresh(model)
    assert model.id is not None

    f = session.exec(select(File).where(File.sha256 == SENTINEL_FILE_HASH)).first()
    if f is None:
        f = File(
            model_id=model.id,
            path="/dev/null",
            original_filename="__external__",
            file_type=FileType.GCODE,
            version=1,
            size_bytes=0,
            sha256=SENTINEL_FILE_HASH,
        )
        session.add(f)
        session.commit()
        session.refresh(f)

    assert f.id is not None
    return f.id, model.id
