#!/usr/bin/env python3
"""
x_autopost.py — Sistema de agendamento e postagem automática no X (Twitter).

Usa a API oficial do X via Tweepy. Fila em SQLite, suporte a threads, imagens,
importação por CSV, limite diário, tentativas automáticas e modo de teste.

Comandos:
  verify                         testa as credenciais
  add "texto" --at "2026-11-20 09:00" [--media foto.jpg]
  import posts.csv               importa vários posts de uma vez
  list [--status pending|posted|failed|canceled]
  cancel ID
  once                           processa o que estiver vencido e sai
  run [--interval 30]            fica rodando e posta nos horários
"""
import argparse
import csv
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import tweepy
from dotenv import load_dotenv

load_dotenv()

TZ = ZoneInfo(os.getenv("TZ_NAME", "America/Sao_Paulo"))
DB_PATH = os.getenv("DB_PATH", "posts.db")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
MAX_PER_DAY = int(os.getenv("MAX_POSTS_PER_DAY", "20"))
MAX_TRIES = 3
THREAD_SEP = "\n---\n"  # separa os tweets de uma thread

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("autopost.log", encoding="utf-8")],
)
log = logging.getLogger("autopost")


# ---------- banco ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute(
        """CREATE TABLE IF NOT EXISTS posts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            media TEXT,
            scheduled_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            tweet_id TEXT,
            error TEXT,
            tries INTEGER NOT NULL DEFAULT 0,
            posted_at TEXT)"""
    )
    return con


# ---------- utilidades ----------
def utcnow():
    return datetime.now(timezone.utc)


def parse_when(s):
    """'now' ou 'AAAA-MM-DD HH:MM' (fuso local) -> ISO em UTC."""
    if not s or s.lower() == "now":
        return utcnow().isoformat()
    dt = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    return dt.astimezone(timezone.utc).isoformat()


def show_local(iso):
    return datetime.fromisoformat(iso).astimezone(TZ).strftime("%d/%m/%Y %H:%M")


def weighted_len(t):
    # o X conta qualquer link como 23 caracteres
    return len(re.sub(r"https?://\S+", "x" * 23, t))


def split_thread(text):
    return [p.strip() for p in text.split(THREAD_SEP) if p.strip()]


def validate(text, media):
    parts = split_thread(text)
    if not parts:
        raise ValueError("texto vazio")
    for i, p in enumerate(parts, 1):
        if weighted_len(p) > 280:
            raise ValueError(f"tweet {i} tem {weighted_len(p)} caracteres (máx. 280)")
    for m in (media or "").split(";"):
        if m.strip() and not os.path.isfile(m.strip()):
            raise ValueError(f"arquivo de mídia não encontrado: {m.strip()}")


# ---------- API do X ----------
def clients():
    keys = {
        "consumer_key": os.getenv("X_API_KEY"),
        "consumer_secret": os.getenv("X_API_SECRET"),
        "access_token": os.getenv("X_ACCESS_TOKEN"),
        "access_token_secret": os.getenv("X_ACCESS_SECRET"),
    }
    if not all(keys.values()):
        sys.exit("Faltam credenciais no .env (veja .env.example).")
    client = tweepy.Client(**keys)
    auth = tweepy.OAuth1UserHandler(*keys.values())
    return client, tweepy.API(auth)


def publish(row):
    """Publica o post (ou thread). Retorna o ID do primeiro tweet."""
    parts = split_thread(row["text"])
    if DRY_RUN:
        log.info("[DRY_RUN] postaria #%s: %s", row["id"], parts[0][:60])
        return "dry-run"
    client, api = clients()
    media_ids = None
    if row["media"]:
        paths = [m.strip() for m in row["media"].split(";") if m.strip()][:4]
        media_ids = [api.media_upload(filename=p).media_id for p in paths]
    first_id, prev = None, None
    for i, part in enumerate(parts):
        resp = client.create_tweet(
            text=part,
            media_ids=media_ids if i == 0 else None,
            in_reply_to_tweet_id=prev,
        )
        prev = str(resp.data["id"])
        first_id = first_id or prev
        time.sleep(1)
    return first_id


def posted_today(con):
    start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    start = start.astimezone(timezone.utc).isoformat()
    return con.execute(
        "SELECT COUNT(*) FROM posts WHERE status='posted' AND posted_at>=?", (start,)
    ).fetchone()[0]


def process_due():
    con = db()
    due = con.execute(
        "SELECT * FROM posts WHERE status='pending' AND scheduled_at<=? ORDER BY scheduled_at",
        (utcnow().isoformat(),),
    ).fetchall()
    for row in due:
        if posted_today(con) >= MAX_PER_DAY:
            log.warning("Limite diário (%s) atingido; o restante fica para amanhã.", MAX_PER_DAY)
            break
        try:
            tid = publish(row)
            con.execute(
                "UPDATE posts SET status='posted', tweet_id=?, posted_at=?, error=NULL WHERE id=?",
                (tid, utcnow().isoformat(), row["id"]),
            )
            log.info("Post #%s publicado (tweet %s)", row["id"], tid)
        except tweepy.TooManyRequests:
            log.warning("Limite de requisições do X. Tento de novo no próximo ciclo.")
            con.commit()
            break
        except Exception as e:  # noqa: BLE001
            tries = row["tries"] + 1
            if tries >= MAX_TRIES:
                con.execute(
                    "UPDATE posts SET status='failed', tries=?, error=? WHERE id=?",
                    (tries, str(e)[:500], row["id"]),
                )
                log.error("Post #%s falhou de vez: %s", row["id"], e)
            else:
                retry = (utcnow() + timedelta(minutes=5 * tries)).isoformat()
                con.execute(
                    "UPDATE posts SET tries=?, error=?, scheduled_at=? WHERE id=?",
                    (tries, str(e)[:500], retry, row["id"]),
                )
                log.warning("Post #%s falhou (%s/%s): %s", row["id"], tries, MAX_TRIES, e)
        con.commit()
    con.close()


# ---------- comandos ----------
def cmd_verify(_):
    client, _api = clients()
    me = client.get_me()
    print(f"OK! Conectado como @{me.data.username}")


def add_post(con, text, when, media):
    text = text.replace("\\n", "\n")
    validate(text, media)
    cur = con.execute(
        "INSERT INTO posts(text, media, scheduled_at) VALUES(?,?,?)",
        (text, media or None, parse_when(when)),
    )
    return cur.lastrowid


def cmd_add(a):
    con = db()
    pid = add_post(con, a.text, a.at, a.media)
    con.commit()
    print(f"Post #{pid} agendado.")


def cmd_import(a):
    con = db()
    ok = bad = 0
    with open(a.file, newline="", encoding="utf-8-sig") as f:
        for n, r in enumerate(csv.DictReader(f), start=2):
            try:
                add_post(con, r["text"], r.get("when"), r.get("media"))
                ok += 1
            except Exception as e:  # noqa: BLE001
                bad += 1
                print(f"Linha {n}: {e}")
    con.commit()
    print(f"{ok} importados, {bad} com erro.")


def cmd_list(a):
    con = db()
    q, args = "SELECT * FROM posts", ()
    if a.status:
        q, args = q + " WHERE status=?", (a.status,)
    for r in con.execute(q + " ORDER BY scheduled_at", args):
        extra = f" | tweet {r['tweet_id']}" if r["tweet_id"] else ""
        err = f" | erro: {r['error']}" if r["error"] and r["status"] != "posted" else ""
        print(f"#{r['id']} [{r['status']}] {show_local(r['scheduled_at'])} — {r['text'][:50]!r}{extra}{err}")


def cmd_cancel(a):
    con = db()
    con.execute("UPDATE posts SET status='canceled' WHERE id=? AND status='pending'", (a.id,))
    con.commit()
    print("Cancelado (se estava pendente).")


def cmd_once(_):
    process_due()


def cmd_run(a):
    log.info("Rodando (verifica a cada %ss). Ctrl+C para parar.%s", a.interval,
             " MODO TESTE." if DRY_RUN else "")
    try:
        while True:
            process_due()
            time.sleep(a.interval)
    except KeyboardInterrupt:
        log.info("Encerrado.")


def main():
    p = argparse.ArgumentParser(description="Postagem automática no X")
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("verify").set_defaults(f=cmd_verify)
    x = s.add_parser("add")
    x.add_argument("text")
    x.add_argument("--at", default="now", help='"AAAA-MM-DD HH:MM" ou now')
    x.add_argument("--media", help="imagens separadas por ; (máx. 4)")
    x.set_defaults(f=cmd_add)
    x = s.add_parser("import")
    x.add_argument("file")
    x.set_defaults(f=cmd_import)
    x = s.add_parser("list")
    x.add_argument("--status")
    x.set_defaults(f=cmd_list)
    x = s.add_parser("cancel")
    x.add_argument("id", type=int)
    x.set_defaults(f=cmd_cancel)
    s.add_parser("once").set_defaults(f=cmd_once)
    x = s.add_parser("run")
    x.add_argument("--interval", type=int, default=30)
    x.set_defaults(f=cmd_run)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
