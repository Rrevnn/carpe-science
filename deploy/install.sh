#!/usr/bin/env bash
# Установка Carpe science на VPS (Ubuntu 22.04+/Debian 12+). Запускать от root из папки /opt/carpe.
set -euo pipefail

APP=/opt/carpe
[ "$(id -u)" = 0 ] || { echo "Запустите от root: sudo bash $0"; exit 1; }
[ -f "$APP/server/server.py" ] || { echo "Не нашла $APP/server/server.py — сначала склонируйте репозиторий в $APP"; exit 1; }

echo "== Carpe science: установка =="
read -rp "Домен сайта без https и www (например carpescience.ru): " DOMAIN
DOMAIN="${DOMAIN#https://}"; DOMAIN="${DOMAIN#http://}"; DOMAIN="${DOMAIN#www.}"; DOMAIN="${DOMAIN%/}"
while true; do
  read -rsp "Придумайте пароль для страницы /admin (не короче 10 символов): " P1; echo
  read -rsp "Повторите пароль: " P2; echo
  [ "$P1" = "$P2" ] && [ ${#P1} -ge 10 ] && break
  echo "Пароли не совпадают или короче 10 символов, попробуйте ещё раз."
done
read -rp "Токен Telegram-бота для уведомлений о заявках (Enter — пропустить): " TG_TOKEN
TG_CHAT=""
if [ -n "$TG_TOKEN" ]; then read -rp "Ваш chat_id в Telegram: " TG_CHAT; fi

echo "== Пакеты =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 git curl gnupg sqlite3 debian-keyring debian-archive-keyring apt-transport-https
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi
python3 -c 'import sys; assert sys.version_info >= (3,10), "нужен Python 3.10+"'

echo "== Пользователь и настройки =="
id carpe >/dev/null 2>&1 || useradd --system --home /var/lib/carpe --shell /usr/sbin/nologin carpe
install -d -o carpe -g carpe -m 750 /var/lib/carpe
install -d -m 750 /etc/carpe
SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
umask 077
P1="$P1" SECRET="$SECRET" APP="$APP" TG_TOKEN="$TG_TOKEN" TG_CHAT="$TG_CHAT" python3 - <<'PY'
import os
q=lambda v:'"'+v.replace('\\','\\\\').replace('"','\\"')+'"'
e=os.environ
lines={"CARPE_ADMIN_PASSWORD":e["P1"],"CARPE_SECRET":e["SECRET"],"CARPE_REPO_DIR":e["APP"],"CARPE_DB":"/var/lib/carpe/carpe.db","CARPE_PORT":"8080","CARPE_TG_TOKEN":e["TG_TOKEN"],"CARPE_TG_CHAT":e["TG_CHAT"]}
open("/etc/carpe/env","w").write("".join(f"{k}={q(v)}\n" for k,v in lines.items()))
PY
umask 022
chmod 600 /etc/carpe/env
unset P1 P2

echo "== Службы =="
cat > /etc/systemd/system/carpe.service <<EOF
[Unit]
Description=Carpe science
After=network.target

[Service]
User=carpe
Group=carpe
EnvironmentFile=/etc/carpe/env
ExecStart=/usr/bin/python3 $APP/server/server.py
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/carpe

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/carpe-pull.service <<EOF
[Unit]
Description=Carpe science: забрать обновления из GitHub

[Service]
Type=oneshot
ExecStart=/bin/bash $APP/deploy/update.sh
EOF

cat > /etc/systemd/system/carpe-pull.timer <<EOF
[Unit]
Description=Carpe science: обновления каждые 10 минут

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/carpe-backup.service <<'EOF'
[Unit]
Description=Carpe science: резервная копия базы

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'install -d -m 700 /var/backups/carpe && sqlite3 /var/lib/carpe/carpe.db ".backup /var/backups/carpe/carpe-$(date +%%F).db" && find /var/backups/carpe -name "carpe-*.db" -mtime +14 -delete'
EOF

cat > /etc/systemd/system/carpe-backup.timer <<'EOF'
[Unit]
Description=Carpe science: копия базы каждую ночь

[Timer]
OnCalendar=*-*-* 04:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	encode zstd gzip
	reverse_proxy 127.0.0.1:8080
	header {
		Strict-Transport-Security "max-age=31536000"
		-Server
	}
}

www.$DOMAIN {
	redir https://$DOMAIN{uri} permanent
}
EOF

# сервер только читает файлы сайта
chmod -R o+rX "$APP"

systemctl daemon-reload
systemctl enable --now carpe.service carpe-pull.timer carpe-backup.timer
systemctl reload caddy || systemctl restart caddy

if command -v ufw >/dev/null; then
  ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null || true
fi

sleep 2
if curl -fsS http://127.0.0.1:8080/api/health >/dev/null; then
  echo
  echo "Готово. Сайт: https://$DOMAIN  ·  админ: https://$DOMAIN/admin"
  echo "Если сайт не открывается, проверьте, что A-записи $DOMAIN и www.$DOMAIN указывают на IP этого сервера."
else
  echo "Сервер не ответил. Журнал: journalctl -u carpe -n 50"
  exit 1
fi
