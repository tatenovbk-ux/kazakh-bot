from datetime import datetime

from pydantic import BaseModel

from app.models import BatchStatus, ItemStatus, TaskType


class BatchCreateResponse(BaseModel):
    id: int
    status: BatchStatus

    class Config:
        from_attributes = True


class WordOut(BaseModel):
    id: int
    kazakh: str
    russian: str
    part_of_speech: str | None
    status: ItemStatus
    is_priority: bool

    class Config:
        from_attributes = True


class WordUpdate(BaseModel):
    kazakh: str | None = None
    russian: str | None = None
    status: ItemStatus | None = None
    is_priority: bool | None = None


class TaskOut(BaseModel):
    id: int
    word_id: int | None
    type: TaskType
    content: dict
    status: ItemStatus

    class Config:
        from_attributes = True


class TaskUpdate(BaseModel):
    content: dict | None = None
    status: ItemStatus | None = None


class BatchDetail(BaseModel):
    id: int
    status: BatchStatus
    source_filename: str | None
    error_message: str | None
    words: list[WordOut]
    tasks: list[TaskOut]

    class Config:
        from_attributes = True


class PinLoginRequest(BaseModel):
    child_id: int
    pin: str


class TaskForChild(BaseModel):
    id: int
    type: TaskType
    content: dict  # answer stripped before sending to the child, see practice router

    class Config:
        from_attributes = True


class AttemptRequest(BaseModel):
    session_id: int
    task_id: int
    answer: str
    response_time_ms: int | None = None


class AttemptResult(BaseModel):
    is_correct: bool
    correct_answer: str
    points_earned: int
    total_points: int


class SessionSummary(BaseModel):
    session_id: int
    tasks_completed: int
    tasks_correct: int
    accuracy_pct: float
    duration_seconds: int
    points_earned: int
    hardest_words: list[str]
