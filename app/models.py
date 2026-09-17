import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class BatchStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY_FOR_REVIEW = "ready_for_review"
    FAILED = "failed"
    DONE = "done"  # all items reviewed (approved or rejected)


class ItemStatus(str, enum.Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


class TaskType(str, enum.Enum):
    TRANSLATE_KK_RU = "translate_kk_ru"
    TRANSLATE_RU_KK = "translate_ru_kk"
    FLASHCARD = "flashcard"
    FILL_BLANK = "fill_blank"
    MULTIPLE_CHOICE = "multiple_choice"
    ANAGRAM = "anagram"
    LISTENING_TEST = "listening_test"
    MATCHING = "matching"
    FILL_IN_THE_BLANK = "fill_in_the_blank"
    TRUE_FALSE = "true_false"
    SENTENCE_BUILDER = "sentence_builder"


class Child(Base):
    __tablename__ = "children"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    pin_hash: Mapped[str] = mapped_column(String(200))
    daily_task_limit: Mapped[int] = mapped_column(Integer, default=15)
    failed_pin_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_points: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    sessions: Mapped[list["PracticeSession"]] = relationship(back_populates="child")


class ContentBatch(Base):
    """One upload from the tutor (a text or a file) awaiting AI parsing + mom's review."""

    __tablename__ = "content_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_text: Mapped[str] = mapped_column(Text)
    status: Mapped[BatchStatus] = mapped_column(Enum(BatchStatus), default=BatchStatus.PENDING)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    words: Mapped[list["Word"]] = relationship(back_populates="batch")
    tasks: Mapped[list["Task"]] = relationship(back_populates="batch")


class Word(Base):
    """A vocabulary unit. One word can back several generated Tasks."""

    __tablename__ = "words"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("content_batches.id"))
    kazakh: Mapped[str] = mapped_column(String(200))
    russian: Mapped[str] = mapped_column(String(200))
    part_of_speech: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[ItemStatus] = mapped_column(Enum(ItemStatus), default=ItemStatus.DRAFT)
    is_priority: Mapped[bool] = mapped_column(Boolean, default=False)  # mom-flagged for extra repetition
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    batch: Mapped["ContentBatch"] = relationship(back_populates="words")
    tasks: Mapped[list["Task"]] = relationship(back_populates="word")


class Task(Base):
    """
    A single exercise. `content` shape depends on `type`:
      translate_kk_ru / translate_ru_kk: {"prompt": "...", "answer": "..."}
      flashcard:                         {"front": "...", "back": "..."}
      fill_blank:                        {"sentence": "Мен мект__ке барамын", "answer": "еп"}
      multiple_choice:                   {"prompt": "...", "options": [...], "answer": "..."}
      anagram:                           {"answer": "мектеп"} (== word_kazakh; letters shuffled at serve time)
      listening_test:                    {"word_kazakh": "...", "options": [4 ru], "answer": "..."}
      fill_in_the_blank:                 {"sentence": "Мен ___ жазамын", "options": [4], "answer": "..."}
      true_false:                        {"kazakh": "...", "shown_translation": "...", "answer": "true"/"false"}
      sentence_builder:                  {"answer": "Мен мектепке барамын"} (words shuffled at serve time)
      matching:                          {"pairs": [{"kazakh": "...", "russian": "..."}, ...]} (synthesized, not Claude-generated)
    """

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("content_batches.id"))
    word_id: Mapped[int | None] = mapped_column(ForeignKey("words.id"), nullable=True)
    type: Mapped[TaskType] = mapped_column(Enum(TaskType))
    content: Mapped[dict] = mapped_column(JSON)
    status: Mapped[ItemStatus] = mapped_column(Enum(ItemStatus), default=ItemStatus.DRAFT)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    batch: Mapped["ContentBatch"] = relationship(back_populates="tasks")
    word: Mapped["Word | None"] = relationship(back_populates="tasks")


class PracticeSession(Base):
    __tablename__ = "practice_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("children.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tasks_planned: Mapped[int] = mapped_column(Integer, default=0)
    tasks_completed: Mapped[int] = mapped_column(Integer, default=0)
    points_earned: Mapped[int] = mapped_column(Integer, default=0)
    report_sent: Mapped[bool] = mapped_column(Boolean, default=False)

    child: Mapped["Child"] = relationship(back_populates="sessions")
    attempts: Mapped[list["TaskAttempt"]] = relationship(back_populates="session")


class TaskAttempt(Base):
    __tablename__ = "task_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("practice_sessions.id"))
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    answer_given: Mapped[str] = mapped_column(String(300))
    is_correct: Mapped[bool] = mapped_column(Boolean)
    response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    answered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped["PracticeSession"] = relationship(back_populates="attempts")
    task: Mapped["Task"] = relationship()
