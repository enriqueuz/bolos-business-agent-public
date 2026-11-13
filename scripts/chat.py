"""Conversa con Bolos desde la terminal usando el simulador de desarrollo (mismo pipeline que WhatsApp).

Uso (dentro de Docker):
  docker compose run --rm api python scripts/chat.py                  # interactivo
  docker compose run --rm api python scripts/chat.py --file demo.txt  # un mensaje por línea

Comandos en modo interactivo:
  /cita N texto   → responde citando la respuesta número N (como "responder" en WhatsApp)
  /salir
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

API = os.environ.get("BOLOS_API", "http://api:8000")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone", default="584140000001")
    parser.add_argument("--name", default="Enrique")
    parser.add_argument("--file")
    args = parser.parse_args()

    replies: list[dict] = []
    client = httpx.Client(base_url=API, timeout=180)

    def send(text: str, reply_to: str | None = None) -> None:
        started = time.monotonic()
        resp = client.post("/api/dev/chat", json={"phone": args.phone, "text": text, "contact_name": args.name,
                                                  "reply_to_provider_message_id": reply_to})
        resp.raise_for_status()
        data = resp.json()
        elapsed = time.monotonic() - started
        for r in data["replies"]:
            replies.append(r)
            print(f"\n🤖 [{len(replies)}] ({data['lane']}, {elapsed:.1f}s)\n{r['text']}")

    def handle(line: str) -> bool:
        line = line.strip()
        if not line or line.startswith("#"):
            return True
        if line == "/salir":
            return False
        print(f"\n👤 {line}")
        if line.startswith("/cita "):
            _, n, text = line.split(" ", 2)
            send(text, replies[int(n) - 1]["provider_message_id"])
        else:
            send(line)
        return True

    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            for line in fh:
                if not handle(line):
                    break
        return
    print("Escribe como si fueras el dueño. /cita N texto para responder citando. /salir para terminar.")
    for line in sys.stdin:
        if not handle(line):
            break


if __name__ == "__main__":
    main()
