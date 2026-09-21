#!/bin/sh
# ---------------------------------------------------------------------
#  CashMash — запуск в один щелчок на macOS и Linux.
#
#  macOS: двойной щелчок по этому файлу в Finder. Если система
#  откажется («неопознанный разработчик») — правый клик → Открыть.
#  Linux: двойной щелчок в файловом менеджере либо ./CashMash.command
#
#  Один раз сделать исполняемым:  chmod +x CashMash.command
# ---------------------------------------------------------------------
cd "$(dirname "$0")" || exit 1

PY=""
for c in ".venv/bin/python" ".venv/Scripts/python.exe" "python3" "python"; do
    if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
    echo "Python не найден. Установите Python 3.12 и повторите."
    read -r _ 2>/dev/null
    exit 1
fi

"$PY" start.py "$@"
echo ""
echo "Окно можно закрыть."
read -r _ 2>/dev/null
