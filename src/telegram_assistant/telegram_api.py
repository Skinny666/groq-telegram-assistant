from __future__ import annotations

import httpx


class TelegramDeliveryError(RuntimeError):
    def __init__(self, *, retry_after_seconds: int | None = None) -> None:
        super().__init__("Falha ao enviar mensagem pelo Telegram.")
        self.retry_after_seconds = retry_after_seconds


async def send_message(
    *,
    bot_token: str,
    chat_id: int,
    text: str,
) -> None:
    if not 1 <= len(text) <= 4000:
        raise ValueError("Mensagem fora do limite seguro.")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers={
                "Accept": "application/json",
                "User-Agent": "telegram-assistant-secure/0.4.0",
            },
        ) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_notification": False,
                    "protect_content": True,
                },
            )
    except httpx.HTTPError as exc:
        raise TelegramDeliveryError() from exc

    if response.status_code == 200:
        return

    retry_after: int | None = None
    if response.status_code == 429 and len(response.content) <= 16 * 1024:
        try:
            payload = response.json()
            raw_retry = payload.get("parameters", {}).get("retry_after")
            if isinstance(raw_retry, int):
                retry_after = max(1, min(raw_retry, 3600))
        except ValueError:
            pass

    raise TelegramDeliveryError(retry_after_seconds=retry_after)
