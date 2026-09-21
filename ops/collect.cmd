@echo off
chcp 65001 > nul
rem ---------------------------------------------------------------------
rem  collect.cmd - непрерывный сбор стакана и ленты Bybit.
rem
rem  Зачем отдельный файл. Сбор L2 - единственный способ получить данные
rem  для последней непроверенной гипотезы: публичного архива стакана у
rem  Bybit нет (см. docs/28). Данные копятся только в реальном времени,
rem  один день за сутки, поэтому процесс должен жить неделями - переживая
rem  закрытие терминала, разрыв сети и перезагрузку машины.
rem
rem  Запускать так:
rem      двойным щелчком по этому файлу, ИЛИ
rem      ops\collect.cmd
rem
rem  Останавливать: Ctrl+C в открывшемся окне.
rem
rem  Чтобы сбор стартовал сам при входе в систему, запустите ОДИН раз:
rem      powershell -ExecutionPolicy Bypass -File ops\install_autostart.ps1
rem ---------------------------------------------------------------------

setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  Не найден .venv\Scripts\python.exe
    echo  Создайте окружение:  python -m venv .venv
    echo.
    pause
    exit /b 1
)

echo.
echo  Сбор стакана и ленты XRPUSDT. Ctrl+C - остановить.
echo  Данные:  data\raw\XRPUSDT\
echo  Логи:    data\logs\collector.log
echo.

rem Супервизор сам перезапускает сборщик при падении и держит счётчик
rem перезапусков: если процесс падает слишком часто, он останавливается,
rem а не молотит в цикле, скрывая настоящую причину.
".venv\Scripts\python.exe" ops\run.py --only collector --symbol XRPUSDT

echo.
echo  Сбор остановлен. Окно останется открытым, чтобы было видно причину.
pause
