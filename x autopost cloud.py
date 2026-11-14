#!/usr/bin/env python3
"""
Versão para nuvem (GitHub Actions). A fila é o arquivo fila.csv.
Colunas: text, when, media, status, tweet_id, posted_at, tries, error
- when: "AAAA-MM-DD HH:MM" no seu fuso (vazio ou "now" = o quanto antes)
- status: deixe vazio para pendente. O robô preenche posted / failed.
Thread: separe os tweets com uma linha contendo só ---
"""
import csv
import os
from datetime import datetime, timezone

import tweepy

import x_autopost as xa

FILE = "fila.csv"
FIELDS = ["text", "when", "media", "status", "tweet_id", "posted_at", "tries", "error"]


def main():
    with open(FILE, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    now = xa.utcnow()
    midnight = datetime.now(xa.TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    midnight = midnight.astimezone(timezone.utc).isoformat()
    today = sum(1 for r in rows if r.get("status") == "posted" and (r.get("posted_at") or "") >= midnight)
    changed = False

    for n, r in enumerate(rows, start=2):
        if (r.get("status") or "").strip().lower() not in ("", "pending"):
            continue
        if not (r.get("text") or "").strip():
            continue
        try:
            due = xa.parse_when(r.get("when"))
        except ValueError:
            r["status"], r["error"], changed = "failed", "data inválida (use AAAA-MM-DD HH:MM)", True
            continue
        if due > now.isoformat():
            continue
        if today >= xa.MAX_PER_DAY:
            xa.log.warning("Limite diário atingido.")
            break
        try:
            xa.validate(r["text"], r.get("media"))
            tid = xa.publish({"id": n, "text": r["text"], "media": r.get("media")})
            r.update(status="posted", tweet_id=tid, posted_at=xa.utcnow().isoformat(), error="")
            today += 1
        except tweepy.TooManyRequests:
            xa.log.warning("Limite de requisições do X; tento no próximo ciclo.")
            break
        except Exception as e:  # noqa: BLE001
            tries = int(r.get("tries") or 0) + 1
            r["tries"], r["error"] = str(tries), str(e)[:300]
            if tries >= xa.MAX_TRIES:
                r["status"] = "failed"
        changed = True

    if changed:
        with open(FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)


if __name__ == "__main__":
    main()
