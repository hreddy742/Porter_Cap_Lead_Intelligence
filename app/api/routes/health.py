from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_session

router = APIRouter()


@router.get("/health")
def health(db: Session = Depends(get_session)):
    try:
        db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        db_status = "error"
    return {"status": "ok", "db": db_status}
