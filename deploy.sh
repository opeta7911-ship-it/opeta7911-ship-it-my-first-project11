#!/bin/bash
# Быстрый деплой на VPS (Ubuntu 22.04)
# Запускать от root: bash deploy.sh

set -e

INSTALL_DIR="/opt/playerok-bot"

echo "==> Установка зависимостей системы..."
apt-get update -q && apt-get install -y python3.11 python3.11-venv git

echo "==> Создание директории..."
mkdir -p "$INSTALL_DIR"

echo "==> Копирование файлов..."
cp -r . "$INSTALL_DIR/"

echo "==> Создание виртуального окружения..."
python3.11 -m venv "$INSTALL_DIR/.venv"

echo "==> Установка Python-зависимостей..."
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

echo "==> Настройка .env..."
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    echo ""
    echo "!! Заполни $INSTALL_DIR/.env и перезапусти: systemctl restart playerok-bot"
fi

echo "==> Установка systemd-сервиса..."
cp "$INSTALL_DIR/playerok-bot.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable playerok-bot

echo ""
echo "Готово! После заполнения .env запусти:"
echo "  systemctl start playerok-bot"
echo "  journalctl -u playerok-bot -f"
