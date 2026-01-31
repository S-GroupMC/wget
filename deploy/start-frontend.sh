#!/bin/bash

# Скрипт запуска фронтенда (Flask + SocketIO)
# Использование: ./start-frontend.sh [порт]

PORT=${1:-5000}
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ADMIN_DIR="$PROJECT_DIR/admin"

echo "==================================="
echo "  Запуск Wget Web Admin"
echo "  Порт: $PORT"
echo "==================================="

cd "$ADMIN_DIR"

# Активация виртуального окружения если есть
if [ -d "venv" ]; then
    echo "Активация виртуального окружения..."
    source venv/bin/activate
fi

# Проверка и установка зависимостей
if [ -f "requirements.txt" ]; then
    echo "Проверка зависимостей..."
    pip install -q -r requirements.txt
fi

# Запуск Flask приложения
echo "Запуск сервера..."
export FLASK_APP=app.py
export FLASK_ENV=development

# Открытие браузера через 2 секунды (в фоне)
(sleep 2 && open "http://127.0.0.1:5050") &

python app.py
