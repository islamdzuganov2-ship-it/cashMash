package com.cashmash.robot

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Подъём после перезагрузки телефона.
 *
 * Робот, переживающий перезагрузку без участия человека, — не удобство,
 * а условие: телефон перезагружается ночью от обновления системы, и
 * молчание до утра означает, что данные за ночь потеряны, а открытая
 * позиция осталась без сопровождения.
 *
 * Автозапуск делается только по явно включённой настройке и только если
 * робот работал до выключения. Приложение, которое само стартует после
 * каждой перезагрузки, — то, что удаляют первым.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action
        if (action != Intent.ACTION_BOOT_COMPLETED &&
            action != Intent.ACTION_MY_PACKAGE_REPLACED) return

        val prefs = Prefs(context)
        if (!prefs.autostart) return
        if (action == Intent.ACTION_BOOT_COMPLETED && !prefs.wasRunning) return

        Log.i("CashMashBoot", "поднимаю робота после $action")
        BotService.start(context)
    }
}
