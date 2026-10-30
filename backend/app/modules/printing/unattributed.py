"""Unattributed."""

from __future__ import annotations

from sqlmodel import Session, col, select

from app.db.models import (
    File,
    FileType,
    Model,
)
from app.db.scopes import live


def artifact_for_printer_filename(
    session: Session, filename: str
) -> tuple[int, int] | None:
    """Return (file_id, model_id) of the live G-code Artifact named ``filename``.

    Printers report only the name a slicer uploaded, so this is a name match,
    not proof of identical bytes. Several matches resolve to the newest upload.
    """
    row = session.exec(
        select(File.id, File.model_id)
        .join(Model, col(Model.id) == File.model_id)
        .where(
            File.original_filename == filename,
            File.file_type == FileType.GCODE,
            live(File),
            live(Model),
        )
        .order_by(col(File.id).desc())
    ).first()
    if row is None or row[0] is None:
        return None
    return int(row[0]), int(row[1])


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
