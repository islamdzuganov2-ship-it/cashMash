package com.cashmash.robot

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities

/**
 * Сеть, за которую платят отдельно.
 *
 * Робот держит открытым поток стакана: это сотни мегабайт в сутки даже
 * без записи на диск. На домашнем Wi-Fi это ничего не стоит, на
 * сотовой связи — счёт, который человек увидит через месяц и не
 * свяжет с приложением.
 *
 * Проверяется НЕ «Wi-Fi ли это». Wi-Fi в поезде или раздача с другого
 * телефона тарифицируются так же, как сотовая связь, и система сама
 * помечает такие сети признаком «тарифицируемая». Спрашивать у неё
 * честнее, чем угадывать по типу подключения.
 *
 * Отсутствие сети тарифицируемым не считается: данные всё равно не
 * идут, а службы умеют переподключаться сами — отказывать из-за
 * секундного провала Wi-Fi значило бы мешать роботу работать.
 */
object NetworkGate {

    /** Известно ли достоверно, что текущая сеть тарифицируется. */
    fun isMetered(context: Context): Boolean {
        val cm = context.getSystemService(ConnectivityManager::class.java)
            ?: return false
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return false
        return !caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED)
    }

    /** Можно ли сейчас работать при заданной настройке. */
    fun allowed(context: Context, prefs: Prefs): Boolean =
        !prefs.unmeteredOnly || !isMetered(context)

    /**
     * Следить за сменой сети, пока робот работает.
     *
     * Возвращает подписку, которую обязан снять вызывающий: подписка,
     * пережившая свой процесс, продолжит будить систему впустую.
     */
    fun watch(context: Context, onMetered: () -> Unit): Watch? {
        val cm = context.getSystemService(ConnectivityManager::class.java)
            ?: return null
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onCapabilitiesChanged(
                network: Network, caps: NetworkCapabilities
            ) {
                val metered =
                    !caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED)
                if (metered) onMetered()
            }
        }
        return runCatching {
            cm.registerDefaultNetworkCallback(callback)
            Watch(cm, callback)
        }.getOrNull()
    }

    class Watch(
        private val cm: ConnectivityManager,
        private val callback: ConnectivityManager.NetworkCallback
    ) {
        fun cancel() {
            runCatching { cm.unregisterNetworkCallback(callback) }
        }
    }
}
