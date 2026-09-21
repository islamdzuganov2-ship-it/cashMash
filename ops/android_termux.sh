#!/data/data/com.termux/files/usr/bin/sh
# ---------------------------------------------------------------------
#  android_termux.sh — робот на телефоне Android.
#
#  Android, в отличие от iOS, позволяет держать долгоживущий процесс:
#  Termux даёт настоящее Linux-окружение с Python, а termux-wake-lock
#  не даёт системе усыпить процесс. Поэтому на Android робот работает
#  ПО-НАСТОЯЩЕМУ — собирает данные, ведёт виртуальную торговлю и
#  показывает панель, — а не просто открывает её с чужого компьютера.
#
#  Что этот скрипт делает:
#    1. ставит Python и зависимости;
#    2. включает wake-lock, чтобы процесс пережил выключение экрана;
#    3. создаёт автозапуск при перезагрузке телефона (нужен Termux:Boot);
#    4. поднимает всё и печатает адрес панели.
#
#  Что нужно ДО запуска:
#    * Termux из F-Droid (версия из Google Play устарела и не обновляется);
#    * Termux:Boot из F-Droid — только для автозапуска, для работы нет;
#    * запустить Termux:Boot один раз, иначе Android не даст ему стартовать.
#
#  Запуск:
#      sh ops/android_termux.sh
#      sh ops/android_termux.sh --no-boot     # без автозапуска
# ---------------------------------------------------------------------

set -e
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"

echo "CashMash · установка на Android (Termux)"
echo "  каталог: $ROOT"
echo ""

# --- 1. зависимости --------------------------------------------------
echo "[1/4] Python и библиотеки"
pkg update -y >/dev/null 2>&1 || true
pkg install -y python termux-api >/dev/null
python -m pip install --upgrade pip >/dev/null
# Сборка колёс на телефоне долгая и часто падает на отсутствии
# компилятора, поэтому ставим только то, без чего не обойтись.
python -m pip install websockets requests pydantic PyYAML >/dev/null
echo "      python $(python -V 2>&1 | cut -d' ' -f2) · библиотеки на месте"

# --- 2. не давать системе усыпить процесс ----------------------------
echo "[2/4] Удержание от сна"
if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock
    echo "      wake-lock включён"
else
    echo "      ⚠ termux-wake-lock не найден — поставьте приложение Termux:API"
    echo "        (F-Droid), иначе Android усыпит процесс через минуты"
fi

# --- 3. автозапуск при перезагрузке ----------------------------------
if [ "$1" != "--no-boot" ]; then
    echo "[3/4] Автозапуск при перезагрузке"
    BOOT="$HOME/.termux/boot"
    mkdir -p "$BOOT"
    cat > "$BOOT/cashmash" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
# Создано ops/android_termux.sh
termux-wake-lock
cd "$ROOT"
python start.py --host 0.0.0.0 --no-browser >> data/logs/android.log 2>&1
EOF
    chmod +x "$BOOT/cashmash"
    echo "      $BOOT/cashmash"
    echo "      ⚠ Требуется приложение Termux:Boot, запущенное хотя бы раз."
else
    echo "[3/4] Автозапуск пропущен (--no-boot)"
fi

# --- 4. запуск -------------------------------------------------------
echo "[4/4] Запуск"
mkdir -p data/logs

IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
[ -z "$IP" ] && IP="127.0.0.1"

echo ""
echo "  Панель: http://$IP:8090   (и http://127.0.0.1:8090 на самом телефоне)"
echo "  Открыв её в Chrome, нажмите «Установить приложение» —"
echo "  панель встанет на домашний экран и будет открываться без браузера."
echo ""
echo "  Остановить: Ctrl+C, затем termux-wake-unlock"
echo ""

exec python start.py --host 0.0.0.0 --no-browser
