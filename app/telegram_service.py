import httpx

from app.config import settings


def send_report(text: str) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return  # not configured yet, silently skip in MVP
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    httpx.post(
        url,
        json={
            "chat_id": settings.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
        },
        timeout=10,
    )
