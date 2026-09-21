# ---------------------------------------------------------------------
#  install_autostart.ps1 — сбор стакана стартует сам при входе в систему.
#
#  ЧТО ЭТОТ СКРИПТ ДЕЛАЕТ С ВАШЕЙ СИСТЕМОЙ (одно действие, обратимое):
#
#      создаёт задачу планировщика Windows с именем CashMashCollector,
#      которая при входе в систему запускает ops\collect.cmd в свёрнутом
#      окне, от вашего имени, без прав администратора.
#
#  Ничего больше не трогается: ни реестр, ни автозагрузка, ни службы,
#  ни сетевые настройки. Задача работает только когда вы вошли в систему.
#
#  УДАЛИТЬ:
#      powershell -ExecutionPolicy Bypass -File ops\install_autostart.ps1 -Remove
#  или через «Планировщик заданий» → CashMashCollector → Удалить.
#
#  ЗАЧЕМ ЭТО НУЖНО. Публичного архива стакана у Bybit нет (docs/28),
#  данные копятся только в реальном времени — один день за сутки. Процесс,
#  запущенный вручную в терминале, умирает вместе с терминалом, и пропуск
#  замечается через неделю, когда выясняется, что данных нет.
#
#  ЗАПУСК:
#      powershell -ExecutionPolicy Bypass -File ops\install_autostart.ps1
# ---------------------------------------------------------------------

param(
    [switch]$Remove,
    [string]$TaskName = "CashMashCollector"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$cmd  = Join-Path $root "ops\collect.cmd"

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Host "Задачи '$TaskName' нет — удалять нечего."
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Задача '$TaskName' удалена. Автозапуск сбора отключён."
    Write-Host "Уже запущенный сбор продолжает работать — закройте его окно вручную."
    exit 0
}

if (-not (Test-Path $cmd)) {
    Write-Host "Не найден $cmd — запускайте скрипт из каталога проекта."
    exit 1
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    Write-Host "Задача '$TaskName' уже существует — пересоздаю с текущими путями."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action  = New-ScheduledTaskAction -Execute "cmd.exe" `
                                   -Argument "/c `"$cmd`"" `
                                   -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Настройки под долгоживущий сбор:
#   StartWhenAvailable  — если машина спала в момент входа, стартовать позже
#   RestartCount/Interval — поднять задачу, если она всё же завершилась
#   ExecutionTimeLimit 0 — НЕ убивать процесс по таймауту (по умолчанию
#                          Windows останавливает задачу через трое суток,
#                          и сбор молча прекратился бы на четвёртый день)
#   DontStopOnIdleEnd / не останавливать при питании от батареи —
#                          иначе сбор прерывается на ноутбуке
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName `
                       -Action $action -Trigger $trigger -Settings $settings `
                       -Description "CashMash: непрерывный сбор стакана и ленты Bybit" | Out-Null

Write-Host ""
Write-Host "Готово. Задача '$TaskName' создана."
Write-Host "  запускает : $cmd"
Write-Host "  когда     : при входе в систему ($env:USERNAME)"
Write-Host "  перезапуск: до 999 раз, интервал 1 мин"
Write-Host "  таймаута выполнения нет — сбор не будет остановлен через трое суток"
Write-Host ""
Write-Host "Запустить прямо сейчас, не выходя из системы:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host ""
Write-Host "Проверить, что сбор идёт:"
Write-Host "  .venv\Scripts\python.exe ops\status.py"
Write-Host ""
Write-Host "Удалить автозапуск:"
Write-Host "  powershell -ExecutionPolicy Bypass -File ops\install_autostart.ps1 -Remove"
