#!/usr/bin/env python3
"""Собирает data/events.json из выгрузки коллекции events (ArtifactData list с out_dir).

Запуск: python3 -I deploy/export_events.py /tmp/export [путь к data/events.json]

Защита: если новых событий заметно меньше, чем уже лежит в файле (или меньше 20),
файл не перезаписывается и скрипт завершается с ошибкой — так случайная пустая
выгрузка не сотрёт расписание на сайте.
"""
import glob
import json
import os
import sys

src = sys.argv[1] if len(sys.argv) > 1 else "/tmp/export"
dst = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "events.json")

files = glob.glob(os.path.join(src, "events", "*.json")) or glob.glob(os.path.join(src, "*.json"))
out = []
for f in files:
    with open(f, encoding="utf-8") as fh:
        j = json.load(fh)
    body = j["data"] if isinstance(j.get("data"), dict) else j
    out.append(dict(body, id=os.path.basename(f)[:-5]))
out = [e for e in out if e.get("d") and e.get("title") and e.get("kind")]
out.sort(key=lambda e: (e["d"], e["id"]))

try:
    with open(dst, encoding="utf-8") as fh:
        before = len(json.load(fh))
except (OSError, ValueError):
    before = 0

if len(out) < 20 or len(out) < before * 0.7:
    sys.exit(f"ОШИБКА: в выгрузке {len(out)} событий, в файле сейчас {before}. Файл НЕ перезаписан, ничего не пушь.")

with open(dst, "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=1)
print(f"Записано событий: {len(out)} (было {before})")
