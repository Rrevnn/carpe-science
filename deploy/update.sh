#!/usr/bin/env bash
# Забирает обновления из GitHub. Запускается таймером каждые 10 минут.
# Новые события (data/events.json) сервер подхватывает сам; при изменении кода сервера он перезапускается.
set -euo pipefail
APP=/opt/carpe
cd "$APP"
before=$(git rev-parse HEAD)
git fetch --quiet origin
git reset --quiet --hard "origin/$(git rev-parse --abbrev-ref HEAD)"
after=$(git rev-parse HEAD)
[ "$before" = "$after" ] && exit 0
chmod -R o+rX "$APP"
if git diff --name-only "$before" "$after" | grep -q '^server/'; then
  systemctl restart carpe.service
fi
echo "Обновлено: ${before:0:7} → ${after:0:7}"
