from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import Base, engine
from app.routers import auth, content, practice, reports

# MVP: create_all instead of Alembic migrations. Switch to Alembic once the
# schema stabilizes and you need to evolve it without dropping data.
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Kazakh Bot")

app.include_router(auth.router)
app.include_router(content.router)
app.include_router(practice.router)
app.include_router(reports.router)

app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")


@app.get("/")
def child_app(request: Request):
    return templates.TemplateResponse(request, "child.html")


@app.get("/admin")
def admin_app(request: Request):
    return templates.TemplateResponse(request, "admin.html")


@app.get("/sw.js")
def service_worker():
    # Served from root (not /static/sw.js) so its default scope covers the
    # whole app ("/"), not just /static/ - required for full PWA offline caching.
    return FileResponse("static/sw.js", media_type="application/javascript")
