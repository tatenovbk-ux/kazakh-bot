import hashlib
import random
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models, schemas
from app.database import get_db
from app.routers.auth import get_current_child
from app.telegram_service import send_report
from app.tts_service import TTSNotConfigured, synthesize_kazakh

router = APIRouter(prefix="/api/practice", tags=["practice"], dependencies=[Depends(get_current_child)])

AUDIO_CACHE_DIR = Path("static/audio")

POINTS_PER_CORRECT = 10
POINTS_PER_ATTEMPT = 2  # small reward just for trying, keeps it encouraging
QUEUE_BATCH_SIZE = 30  # tasks served per session - practice is unlimited, this just sizes one batch


def _tasks_completed_today(child_id: int, db: Session) -> int:
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    total = db.scalar(
        select(func.coalesce(func.sum(models.PracticeSession.tasks_completed), 0)).where(
            models.PracticeSession.child_id == child_id,
            models.PracticeSession.started_at >= today_start,
        )
    )
    return total or 0


@router.get("/queue", response_model=list[schemas.TaskForChild])
def get_queue(child: models.Child = Depends(get_current_child), db: Session = Depends(get_db)):
    # Words the child has gotten wrong recently surface first (lightweight Leitner-style priority),
    # same as words mom has manually flagged as needing extra repetition.
    wrong_word_ids = db.scalars(
        select(models.Task.word_id)
        .join(models.TaskAttempt, models.TaskAttempt.task_id == models.Task.id)
        .join(models.PracticeSession, models.PracticeSession.id == models.TaskAttempt.session_id)
        .where(
            models.PracticeSession.child_id == child.id,
            models.TaskAttempt.is_correct.is_(False),
            models.TaskAttempt.answered_at >= datetime.utcnow() - timedelta(days=7),
        )
        .distinct()
    ).all()
    priority_word_ids = db.scalars(select(models.Word.id).where(models.Word.is_priority.is_(True))).all()
    priority_word_id_set = set(wrong_word_ids) | set(priority_word_ids)

    approved = select(models.Task).where(models.Task.status == models.ItemStatus.APPROVED)

    priority_tasks = []
    if priority_word_id_set:
        priority_tasks = db.scalars(
            approved.where(models.Task.word_id.in_(priority_word_id_set))
            .order_by(func.random())
            .limit(QUEUE_BATCH_SIZE)
        ).all()

    remaining_slots = QUEUE_BATCH_SIZE - len(priority_tasks)
    fresh_tasks = []
    if remaining_slots > 0:
        exclude_ids = [t.id for t in priority_tasks]
        fresh_query = approved.order_by(func.random()).limit(remaining_slots)
        if exclude_ids:
            fresh_query = fresh_query.where(models.Task.id.notin_(exclude_ids))
        fresh_tasks = db.scalars(fresh_query).all()

    tasks = [*priority_tasks, *fresh_tasks]

    out = []
    for t in tasks:
        if t.type in (models.TaskType.FLASHCARD, models.TaskType.MATCHING):
            # Flashcards are self-graded: the "answer" (back of the card) is
            # content the child is meant to see, not a guess to strip.
            # Matching has no "answer" key at all - the pairs themselves are the exercise.
            safe_content = t.content
        elif t.type == models.TaskType.ANAGRAM:
            safe_content = {"letters": _shuffled(list(t.content["answer"]))}
        elif t.type == models.TaskType.SENTENCE_BUILDER:
            safe_content = {"words": _shuffled(t.content["answer"].split())}
        else:
            safe_content = {k: v for k, v in t.content.items() if k != "answer"}
        out.append(schemas.TaskForChild(id=t.id, type=t.type, content=safe_content))
    return out


def _shuffled(items: list) -> list:
    """Shuffles a copy, re-rolling once if it happened to land back in the original order."""
    shuffled = items[:]
    random.shuffle(shuffled)
    if len(items) > 1 and shuffled == items:
        random.shuffle(shuffled)
    return shuffled


@router.get("/audio")
def get_audio(text: str):
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")

    AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    path = AUDIO_CACHE_DIR / f"{cache_key}.mp3"

    if not path.exists():
        try:
            audio_bytes = synthesize_kazakh(text)
        except TTSNotConfigured as exc:
            raise HTTPException(status_code=503, detail="Text-to-speech is not configured") from exc
        except Exception as exc:  # noqa: BLE001 - surface upstream Azure failures as a clean 502
            raise HTTPException(status_code=502, detail="Text-to-speech request failed") from exc
        path.write_bytes(audio_bytes)

    return FileResponse(path, media_type="audio/mpeg")


@router.get("/home")
def get_home(child: models.Child = Depends(get_current_child), db: Session = Depends(get_db)):
    return {
        "name": child.name,
        "total_points": child.total_points,
        "completed_today": _tasks_completed_today(child.id, db),
    }


@router.post("/session/start")
def start_session(child: models.Child = Depends(get_current_child), db: Session = Depends(get_db)):
    session = models.PracticeSession(child_id=child.id, tasks_planned=QUEUE_BATCH_SIZE)
    db.add(session)
    db.commit()
    db.refresh(session)
    return {"session_id": session.id, "tasks_planned": QUEUE_BATCH_SIZE}


@router.post("/attempt", response_model=schemas.AttemptResult)
def submit_attempt(
    payload: schemas.AttemptRequest,
    child: models.Child = Depends(get_current_child),
    db: Session = Depends(get_db),
):
    session = db.get(models.PracticeSession, payload.session_id)
    task = db.get(models.Task, payload.task_id)
    if not session or session.child_id != child.id or not task:
        raise HTTPException(status_code=404, detail="Session or task not found")
    if session.ended_at is not None:
        raise HTTPException(status_code=400, detail="Session already ended")

    if task.type == models.TaskType.FLASHCARD:
        # Self-graded: frontend sends "known" or "unknown" after the child flips the card.
        is_correct = payload.answer.strip().lower() == "known"
    elif task.type == models.TaskType.MATCHING:
        # Self-graded: the frontend only submits once every pair has been matched correctly.
        is_correct = True
    else:
        correct_answer = str(task.content.get("answer", "")).strip().lower()
        is_correct = payload.answer.strip().lower() == correct_answer

    points = POINTS_PER_ATTEMPT + (POINTS_PER_CORRECT if is_correct else 0)

    db.add(
        models.TaskAttempt(
            session_id=session.id,
            task_id=task.id,
            answer_given=payload.answer,
            is_correct=is_correct,
            response_time_ms=payload.response_time_ms,
        )
    )
    session.tasks_completed += 1
    session.points_earned += points
    child.total_points += points
    db.commit()

    return schemas.AttemptResult(
        is_correct=is_correct,
        correct_answer=task.content.get("answer", ""),
        points_earned=points,
        total_points=child.total_points,
    )


@router.post("/session/{session_id}/end", response_model=schemas.SessionSummary)
def end_session(session_id: int, child: models.Child = Depends(get_current_child), db: Session = Depends(get_db)):
    session = db.get(models.PracticeSession, session_id)
    if not session or session.child_id != child.id:
        raise HTTPException(status_code=404, detail="Session not found")

    session.ended_at = datetime.utcnow()
    db.commit()

    attempts = session.attempts
    correct = sum(1 for a in attempts if a.is_correct)
    duration = int((session.ended_at - session.started_at).total_seconds())

    hardest = (
        db.query(models.Word.kazakh)
        .join(models.Task, models.Task.word_id == models.Word.id)
        .join(models.TaskAttempt, models.TaskAttempt.task_id == models.Task.id)
        .filter(models.TaskAttempt.session_id == session.id, models.TaskAttempt.is_correct.is_(False))
        .distinct()
        .limit(5)
        .all()
    )
    hardest_words = [w[0] for w in hardest]

    summary = schemas.SessionSummary(
        session_id=session.id,
        tasks_completed=session.tasks_completed,
        tasks_correct=correct,
        accuracy_pct=round(100 * correct / len(attempts), 1) if attempts else 0.0,
        duration_seconds=duration,
        points_earned=session.points_earned,
        hardest_words=hardest_words,
    )

    if not session.report_sent:
        text = (
            f"🌟 {child.name} позанималась казахским!\n"
            f"Заданий: {summary.tasks_completed}, верно: {summary.tasks_correct} "
            f"({summary.accuracy_pct}%)\n"
            f"Время: {summary.duration_seconds // 60} мин\n"
            f"Кристаллы: +{summary.points_earned} (всего {child.total_points})\n"
            + (f"Сложнее всего дались: {', '.join(hardest_words)}" if hardest_words else "Все слова легко дались!")
        )
        send_report(text)
        session.report_sent = True
        db.commit()

    return summary
