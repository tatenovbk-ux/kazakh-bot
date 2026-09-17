from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.analytics import build_dashboard
from app.database import get_db
from app.routers.content import require_admin

router = APIRouter(prefix="/api/admin/reports", tags=["reports"], dependencies=[Depends(require_admin)])


@router.get("/dashboard")
def get_dashboard(child_id: int, db: Session = Depends(get_db)):
    dashboard = build_dashboard(child_id, db)
    if dashboard is None:
        raise HTTPException(status_code=404, detail="Child not found")
    return dashboard


@router.get("/sessions")
def list_sessions(child_id: int, db: Session = Depends(get_db)):
    sessions = db.scalars(
        select(models.PracticeSession)
        .where(models.PracticeSession.child_id == child_id)
        .order_by(models.PracticeSession.started_at.desc())
        .limit(30)
    ).all()
    return [
        {
            "id": s.id,
            "started_at": s.started_at,
            "ended_at": s.ended_at,
            "tasks_completed": s.tasks_completed,
            "points_earned": s.points_earned,
        }
        for s in sessions
    ]
