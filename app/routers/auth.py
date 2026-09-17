from datetime import datetime, timedelta

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app import models, schemas
from app.config import settings
from app.database import get_db
from app.security import verify_pin

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/children")
def list_children(db: Session = Depends(get_db)):
    """Public: just names + ids, so the login screen can show tappable avatars instead of asking for an ID."""
    children = db.query(models.Child).all()
    return [{"id": c.id, "name": c.name} for c in children]

serializer = URLSafeTimedSerializer(settings.secret_key, salt="child-session")
SESSION_COOKIE = "child_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30  # 30 days - PIN should be rare, not per-action
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


@router.post("/pin")
def login_with_pin(payload: schemas.PinLoginRequest, response: Response, db: Session = Depends(get_db)):
    child = db.get(models.Child, payload.child_id)
    if not child:
        raise HTTPException(status_code=404, detail="Unknown child")

    if child.locked_until and child.locked_until > datetime.utcnow():
        raise HTTPException(status_code=423, detail="Too many attempts, try again later")

    if not verify_pin(payload.pin, child.pin_hash):
        child.failed_pin_attempts += 1
        if child.failed_pin_attempts >= MAX_FAILED_ATTEMPTS:
            child.locked_until = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
            child.failed_pin_attempts = 0
        db.commit()
        raise HTTPException(status_code=401, detail="Wrong PIN")

    child.failed_pin_attempts = 0
    db.commit()

    token = serializer.dumps({"child_id": child.id})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return {"child_id": child.id, "name": child.name}


def get_current_child(
    child_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    db: Session = Depends(get_db),
) -> models.Child:
    if not child_session:
        raise HTTPException(status_code=401, detail="Not logged in")
    try:
        data = serializer.loads(child_session, max_age=SESSION_MAX_AGE_SECONDS)
    except BadSignature as exc:
        raise HTTPException(status_code=401, detail="Invalid session") from exc

    child = db.get(models.Child, data["child_id"])
    if not child:
        raise HTTPException(status_code=401, detail="Invalid session")
    return child
