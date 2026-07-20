#!/usr/bin/env python3
from __future__ import annotations

from getpass import getpass
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> None:
    token = getpass("Token do bot Telegram: ").strip()
    if not token or any(character.isspace() for character in token):
        raise SystemExit("Token inválido.")

    print("Envie /start em uma conversa privada com o bot e pressione Enter.")
    input()

    url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=1&allowed_updates=%5B%22message%22%5D"
    request = Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "User-Agent": "telegram-assistant-id-tool/0.1",
        },
    )

    try:
        with urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise SystemExit("Telegram retornou erro.")
            body = response.read(128 * 1024)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise SystemExit(f"Falha ao consultar Telegram: {type(exc).__name__}") from exc

    payload = json.loads(body)
    found: set[int] = set()
    for update in payload.get("result", []):
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat", {})
        user = message.get("from", {})
        user_id = user.get("id")
        if chat.get("type") == "private" and isinstance(user_id, int):
            found.add(user_id)

    if not found:
        raise SystemExit(
            "Nenhum usuário privado encontrado. Envie /start e tente novamente."
        )

    print("Telegram user IDs encontrados:")
    for user_id in sorted(found):
        print(user_id)


if __name__ == "__main__":
    main()
