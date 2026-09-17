"""Builds the parent-facing progress dashboard by aggregating existing attempt/session data.

No dedicated analytics tables - everything here is computed on the fly from Word/Task/
TaskAttempt/PracticeSession. The dataset is small (a family app), so grouping in Python
after a couple of simple queries is simpler and safer than window-function SQL.
"""

import random
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models

MASTERY_MIN_ATTEMPTS = 3
MASTERY_WINDOW_DAYS = 14
WEAK_WORD_MIN_ATTEMPTS = 2
WEAK_WORD_ACCURACY_THRESHOLD = 70.0
ACTIVITY_WINDOW_DAYS = 14
AVG_SESSION_WINDOW_DAYS = 30

SPECIAL_LETTERS = "әіүөқғңұ"

# Types where the child free-types Kazakh text, so a wrong answer can be diffed
# letter-by-letter against the correct one. Selection-based types (multiple_choice,
# listening_test, fill_in_the_blank, true_false) don't carry spelling information.
FREE_TEXT_KAZAKH_TYPES = {
    models.TaskType.FILL_BLANK,
    models.TaskType.ANAGRAM,
    models.TaskType.SENTENCE_BUILDER,
    models.TaskType.TRANSLATE_RU_KK,
}

GRAMMAR_CASE_TYPES = {models.TaskType.FILL_IN_THE_BLANK, models.TaskType.SENTENCE_BUILDER}


def build_dashboard(child_id: int, db: Session) -> dict | None:
    child = db.get(models.Child, child_id)
    if not child:
        return None

    attempts = _child_attempts(child_id, db)

    vocabulary = _vocabulary_progress(attempts, db)
    weak_words = _weak_words(attempts, db)
    letter_confusions = _letter_confusions(attempts)
    grammar_case_errors = _grammar_case_errors(attempts)
    activity = _activity(child_id, db)

    return {
        "child": {"name": child.name, "total_points": child.total_points},
        "vocabulary": vocabulary,
        "weak_words": weak_words,
        "letter_confusions": letter_confusions,
        "grammar_case_errors": grammar_case_errors,
        "activity": activity,
        "summary_text": _summary_text(child, vocabulary, weak_words, letter_confusions, activity),
    }


def _child_attempts(child_id: int, db: Session) -> list[tuple[models.TaskAttempt, models.Task]]:
    rows = db.execute(
        select(models.TaskAttempt, models.Task)
        .join(models.Task, models.Task.id == models.TaskAttempt.task_id)
        .join(models.PracticeSession, models.PracticeSession.id == models.TaskAttempt.session_id)
        .where(models.PracticeSession.child_id == child_id)
        .order_by(models.TaskAttempt.answered_at.asc())
    ).all()
    return [(row[0], row[1]) for row in rows]


def _vocabulary_progress(attempts: list[tuple[models.TaskAttempt, models.Task]], db: Session) -> dict:
    by_word: dict[int, list[models.TaskAttempt]] = defaultdict(list)
    for attempt, task in attempts:
        if task.word_id is not None:
            by_word[task.word_id].append(attempt)

    word_ids = db.scalars(select(models.Word.id).where(models.Word.status == models.ItemStatus.APPROVED)).all()

    cutoff = datetime.utcnow() - timedelta(days=MASTERY_WINDOW_DAYS)
    new_count = learning_count = mastered_count = 0

    for word_id in word_ids:
        word_attempts = by_word.get(word_id, [])
        if not word_attempts:
            new_count += 1
            continue
        last_attempts = word_attempts[-MASTERY_MIN_ATTEMPTS:]
        recent_wrong = any(not a.is_correct and a.answered_at >= cutoff for a in word_attempts)
        if len(word_attempts) >= MASTERY_MIN_ATTEMPTS and all(a.is_correct for a in last_attempts) and not recent_wrong:
            mastered_count += 1
        else:
            learning_count += 1

    return {"new": new_count, "learning": learning_count, "mastered": mastered_count, "total": len(word_ids)}


def _weak_words(attempts: list[tuple[models.TaskAttempt, models.Task]], db: Session) -> list[dict]:
    results_by_word: dict[int, list[bool]] = defaultdict(list)
    for attempt, task in attempts:
        if task.word_id is not None:
            results_by_word[task.word_id].append(attempt.is_correct)

    candidate_ids = [wid for wid, results in results_by_word.items() if len(results) >= WEAK_WORD_MIN_ATTEMPTS]
    if not candidate_ids:
        return []
    words = {w.id: w for w in db.scalars(select(models.Word).where(models.Word.id.in_(candidate_ids)))}

    weak = []
    for word_id in candidate_ids:
        word = words.get(word_id)
        if not word:
            continue
        results = results_by_word[word_id]
        accuracy = 100 * sum(results) / len(results)
        if accuracy < WEAK_WORD_ACCURACY_THRESHOLD:
            weak.append(
                {
                    "kazakh": word.kazakh,
                    "russian": word.russian,
                    "accuracy_pct": round(accuracy, 1),
                    "attempts": len(results),
                }
            )

    weak.sort(key=lambda w: (w["accuracy_pct"], -w["attempts"]))
    return weak[:8]


def _letter_confusions(attempts: list[tuple[models.TaskAttempt, models.Task]]) -> list[dict]:
    counts: Counter = Counter()
    for attempt, task in attempts:
        if attempt.is_correct or task.type not in FREE_TEXT_KAZAKH_TYPES:
            continue
        correct = str(task.content.get("answer", "")).lower()
        given = attempt.answer_given.lower()
        for letter in SPECIAL_LETTERS:
            if correct.count(letter) > given.count(letter):
                counts[letter] += 1

    return [{"letter": letter, "count": n} for letter, n in counts.most_common(6)]


def _grammar_case_errors(attempts: list[tuple[models.TaskAttempt, models.Task]]) -> list[dict]:
    stats: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "wrong": 0})
    for attempt, task in attempts:
        if task.type not in GRAMMAR_CASE_TYPES:
            continue
        case = task.content.get("grammar_case")
        if not case:
            continue
        stats[case]["total"] += 1
        if not attempt.is_correct:
            stats[case]["wrong"] += 1

    errors = []
    for case, s in stats.items():
        if s["wrong"] == 0:
            continue
        errors.append(
            {
                "case": case,
                "count": s["wrong"],
                "accuracy_pct": round(100 * (s["total"] - s["wrong"]) / s["total"], 1),
            }
        )
    errors.sort(key=lambda e: -e["count"])
    return errors[:5]


def _activity(child_id: int, db: Session) -> dict:
    today = datetime.utcnow().date()
    window_start = today - timedelta(days=ACTIVITY_WINDOW_DAYS - 1)

    sessions = db.scalars(
        select(models.PracticeSession)
        .where(models.PracticeSession.child_id == child_id)
        .order_by(models.PracticeSession.started_at.asc())
    ).all()

    by_date: dict = defaultdict(lambda: {"tasks_completed": 0, "seconds": 0.0})
    active_days: set = set()
    for s in sessions:
        if s.tasks_completed <= 0:
            continue
        day = s.started_at.date()
        active_days.add(day)
        by_date[day]["tasks_completed"] += s.tasks_completed
        if s.ended_at:
            by_date[day]["seconds"] += (s.ended_at - s.started_at).total_seconds()

    daily = []
    day = window_start
    while day <= today:
        entry = by_date.get(day, {"tasks_completed": 0, "seconds": 0.0})
        daily.append(
            {
                "date": day.isoformat(),
                "tasks_completed": entry["tasks_completed"],
                "minutes": round(entry["seconds"] / 60, 1),
            }
        )
        day += timedelta(days=1)

    current_streak = _streak_from(today, active_days)
    if current_streak == 0:
        # Give credit for yesterday's streak even before today's session happens.
        current_streak = _streak_from(today - timedelta(days=1), active_days)

    best_streak = current_streak
    run = 0
    prev_day = None
    for d in sorted(active_days):
        run = run + 1 if prev_day == d - timedelta(days=1) else 1
        best_streak = max(best_streak, run)
        prev_day = d

    avg_window_start = datetime.utcnow() - timedelta(days=AVG_SESSION_WINDOW_DAYS)
    ended_sessions = [s for s in sessions if s.ended_at and s.started_at >= avg_window_start]
    avg_minutes = 0.0
    if ended_sessions:
        avg_seconds = sum((s.ended_at - s.started_at).total_seconds() for s in ended_sessions) / len(ended_sessions)
        avg_minutes = round(avg_seconds / 60, 1)

    return {
        "daily": daily,
        "current_streak_days": current_streak,
        "best_streak_days": best_streak,
        "avg_session_minutes": avg_minutes,
    }


def _streak_from(start_day, active_days: set) -> int:
    streak = 0
    d = start_day
    while d in active_days:
        streak += 1
        d -= timedelta(days=1)
    return streak


def _summary_text(child, vocabulary: dict, weak_words: list[dict], letter_confusions: list[dict], activity: dict) -> str:
    parts = []
    name = child.name
    streak = activity["current_streak_days"]

    if streak >= 3:
        parts.append(
            random.choice(
                [
                    f"{name} занимается уже {streak} дней подряд — отличная привычка! 🔥",
                    f"{streak} дней подряд без пропусков — {name} держит темп! 🔥",
                ]
            )
        )
    elif streak >= 1:
        parts.append(f"{name} только набирает темп ({streak} {'день' if streak == 1 else 'дня'} подряд) — продолжаем!")
    elif not activity["daily"][-1]["tasks_completed"] and not activity["daily"][-2]["tasks_completed"]:
        parts.append(f"{name} давно не занималась — может, стоит напомнить про казахский сегодня?")

    if vocabulary["mastered"] > 0:
        parts.append(
            random.choice(
                [
                    f"В активном словарном запасе уже {vocabulary['mastered']} слов, "
                    f"ещё {vocabulary['learning']} — на подходе.",
                    f"{vocabulary['mastered']} слов прочно усвоены, {vocabulary['learning']} — в процессе повторения.",
                ]
            )
        )
    elif vocabulary["learning"] > 0:
        parts.append(f"Пока {vocabulary['learning']} слов в работе — со временем они перейдут в актив.")
    else:
        parts.append("Занятия только начинаются — прогресс появится после первых сессий.")

    if weak_words:
        w = weak_words[0]
        parts.append(f"Стоит повторить слово «{w['kazakh']}» ({w['russian']}) — пока даётся непросто.")

    if letter_confusions:
        letters = ", ".join(c["letter"] for c in letter_confusions[:3])
        parts.append(f"Чаще всего путаются буквы: {letters} — можно потренировать их отдельно.")

    return " ".join(parts)
