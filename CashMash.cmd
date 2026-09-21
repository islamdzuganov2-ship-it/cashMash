@echo off
chcp 65001 > nul
rem ---------------------------------------------------------------------
rem  CashMash - запуск в один щелчок на Windows.
rem
rem  Двойной щелчок по этому файлу поднимает всё: сбор данных,
rem  виртуальную торговлю, наблюдатель новостей, алерты и панель,
rem  а затем открывает панель в браузере.
rem
rem  Чтобы вынести на рабочий стол: правый клик по файлу ->
rem  "Отправить" -> "Рабочий стол (создать ярлык)".
rem
rem  Ctrl+C в открывшемся окне останавливает всё сразу.
rem ---------------------------------------------------------------------

setlocal
cd /d "%~dp0"

set PY=
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
if not defined PY if exist ".venv\bin\python.exe" set PY=.venv\bin\python.exe
if not defined PY (
    where python >nul 2>&1 && set PY=python
)

if not defined PY (
    echo.
    echo  Python не найден.
    echo  Установите Python 3.12 с python.org и повторите.
    echo.
    pause
    exit /b 1
)

"%PY%" start.py %*

echo.
echo  Остановлено. Окно останется открытым, чтобы было видно причину.
pause
