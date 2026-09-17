from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, schemas
from app.claude_service import generate_tasks_from_text
from app.config import settings
from app.database import get_db
from app.file_extract import extract_text
from app.security import hash_pin

router = APIRouter(prefix="/api/admin", tags=["content"])


def require_admin(x_admin_token: str = Header(...)) -> None:
    if x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid admin token")


def _run_generation(batch: models.ContentBatch, db: Session) -> None:
    """Calls Claude and populates draft Word/Task rows. Synchronous for MVP simplicity."""
    batch.status = models.BatchStatus.PROCESSING
    db.commit()

    try:
        result = generate_tasks_from_text(batch.raw_text)
    except Exception as exc:  # noqa: BLE001 - surface any Claude/parsing failure to the admin
        batch.status = models.BatchStatus.FAILED
        batch.error_message = str(exc)
        db.commit()
        return

    tasks_data = result.get("tasks", [])

    word_by_kazakh: dict[str, models.Word] = {}
    for w in result.get("words", []):
        word = models.Word(
            batch_id=batch.id,
            kazakh=w["kazakh"],
            russian=w["russian"],
            part_of_speech=w.get("part_of_speech"),
        )
        db.add(word)
        db.flush()  # get word.id without a full commit
        word_by_kazakh[w["kazakh"]] = word

    # Claude's forced tool-use only guarantees schema shape, not that every
    # required array is populated - it has been observed to return tasks
    # while leaving `words` empty. Reconstruct anything missing from the
    # tasks themselves so words never silently disappear from moderation.
    missing_kazakh = {t["word_kazakh"] for t in tasks_data} - set(word_by_kazakh)
    if missing_kazakh:
        russian_by_kazakh: dict[str, str] = {}
        for t in tasks_data:
            kk = t["word_kazakh"]
            if kk not in missing_kazakh or kk in russian_by_kazakh:
                continue
            content = t["content"]
            if t["type"] == "flashcard" and content.get("front") == kk:
                russian_by_kazakh[kk] = content.get("answer", "")
            elif t["type"] == "translate_kk_ru" and content.get("prompt") == kk:
                russian_by_kazakh[kk] = content.get("answer", "")
            elif t["type"] == "translate_ru_kk" and content.get("answer") == kk:
                russian_by_kazakh[kk] = content.get("prompt", "")

        for kk in missing_kazakh:
            word = models.Word(batch_id=batch.id, kazakh=kk, russian=russian_by_kazakh.get(kk, ""))
            db.add(word)
            db.flush()
            word_by_kazakh[kk] = word

    for t in tasks_data:
        word = word_by_kazakh.get(t["word_kazakh"])
        db.add(
            models.Task(
                batch_id=batch.id,
                word_id=word.id if word else None,
                type=models.TaskType(t["type"]),
                content=t["content"],
            )
        )

    _synthesize_matching_tasks(batch, word_by_kazakh, db)

    batch.status = models.BatchStatus.READY_FOR_REVIEW
    db.commit()


MATCHING_GROUP_SIZE = 4


def _synthesize_matching_tasks(
    batch: models.ContentBatch, word_by_kazakh: dict[str, models.Word], db: Session
) -> None:
    """Builds 'find the pair' tasks by grouping words, instead of asking Claude for them.

    A matching task covers several words at once, which doesn't fit the tool schema's
    one-task-per-word_kazakh shape - so it's assembled here from words Claude already
    extracted, guaranteeing valid pairs with no extra API cost.
    """
    words = [w for w in word_by_kazakh.values() if w.russian]
    for i in range(0, len(words) - 1, MATCHING_GROUP_SIZE):
        group = words[i : i + MATCHING_GROUP_SIZE]
        if len(group) < 2:
            continue
        db.add(
            models.Task(
                batch_id=batch.id,
                word_id=None,
                type=models.TaskType.MATCHING,
                content={"pairs": [{"kazakh": w.kazakh, "russian": w.russian} for w in group]},
            )
        )


class TextBatchCreate(BaseModel):
    raw_text: str


@router.post("/batches/text", response_model=schemas.BatchCreateResponse, dependencies=[Depends(require_admin)])
def create_batch_from_text(payload: TextBatchCreate, db: Session = Depends(get_db)):
    """MVP entry point: mom pastes the tutor's text directly."""
    batch = models.ContentBatch(raw_text=payload.raw_text)
    db.add(batch)
    db.commit()
    db.refresh(batch)

    _run_generation(batch, db)
    db.refresh(batch)
    return batch


@router.post("/batches/upload", response_model=schemas.BatchCreateResponse, dependencies=[Depends(require_admin)])
async def create_batch_from_file(file: UploadFile, db: Session = Depends(get_db)):
    content = await file.read()
    try:
        text = extract_text(file.filename, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    batch = models.ContentBatch(raw_text=text, source_filename=file.filename)
    db.add(batch)
    db.commit()
    db.refresh(batch)

    _run_generation(batch, db)
    db.refresh(batch)
    return batch


@router.get("/batches", dependencies=[Depends(require_admin)])
def list_batches(db: Session = Depends(get_db)):
    batches = db.query(models.ContentBatch).order_by(models.ContentBatch.uploaded_at.desc()).all()
    return [
        {
            "id": b.id,
            "status": b.status,
            "source_filename": b.source_filename,
            "uploaded_at": b.uploaded_at,
            "word_count": len(b.words),
            "task_count": len(b.tasks),
        }
        for b in batches
    ]


@router.get("/batches/{batch_id}", response_model=schemas.BatchDetail, dependencies=[Depends(require_admin)])
def get_batch(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(models.ContentBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return batch


@router.get("/words", response_model=list[schemas.WordOut], dependencies=[Depends(require_admin)])
def list_words(db: Session = Depends(get_db)):
    """All approved vocabulary, for the parent's priority-word management view."""
    return db.scalars(
        select(models.Word)
        .where(models.Word.status == models.ItemStatus.APPROVED)
        .order_by(models.Word.is_priority.desc(), models.Word.kazakh)
    ).all()


@router.patch("/words/{word_id}", response_model=schemas.WordOut, dependencies=[Depends(require_admin)])
def update_word(word_id: int, payload: schemas.WordUpdate, db: Session = Depends(get_db)):
    word = db.get(models.Word, word_id)
    if not word:
        raise HTTPException(status_code=404, detail="Word not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(word, field, value)
    db.commit()
    db.refresh(word)
    return word


@router.patch("/tasks/{task_id}", response_model=schemas.TaskOut, dependencies=[Depends(require_admin)])
def update_task(task_id: int, payload: schemas.TaskUpdate, db: Session = Depends(get_db)):
    task = db.get(models.Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(task, field, value)
    db.commit()
    db.refresh(task)
    return task


class ChildCreate(BaseModel):
    name: str
    pin: str
    daily_task_limit: int = settings.daily_task_limit_default


@router.get("/children", dependencies=[Depends(require_admin)])
def list_children(db: Session = Depends(get_db)):
    children = db.query(models.Child).all()
    return [
        {"id": c.id, "name": c.name, "total_points": c.total_points, "daily_task_limit": c.daily_task_limit}
        for c in children
    ]


@router.post("/children", dependencies=[Depends(require_admin)])
def create_child(payload: ChildCreate, db: Session = Depends(get_db)):
    if not (payload.pin.isdigit() and len(payload.pin) == 4):
        raise HTTPException(status_code=400, detail="PIN must be exactly 4 digits")
    child = models.Child(
        name=payload.name,
        pin_hash=hash_pin(payload.pin),
        daily_task_limit=payload.daily_task_limit,
    )
    db.add(child)
    db.commit()
    db.refresh(child)
    return {"id": child.id, "name": child.name}


@router.post("/batches/{batch_id}/approve-all", dependencies=[Depends(require_admin)])
def approve_all(batch_id: int, db: Session = Depends(get_db)):
    """Convenience bulk-approve for everything still in draft status."""
    batch = db.get(models.ContentBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    for word in batch.words:
        if word.status == models.ItemStatus.DRAFT:
            word.status = models.ItemStatus.APPROVED
    for task in batch.tasks:
        if task.status == models.ItemStatus.DRAFT:
            task.status = models.ItemStatus.APPROVED

    batch.status = models.BatchStatus.DONE
    db.commit()
    return {"ok": True}
