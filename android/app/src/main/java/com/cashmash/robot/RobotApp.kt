package com.cashmash.robot

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.os.Build

/**
 * Приложение целиком. Делает ровно две вещи: заводит канал уведомлений
 * и ничего не запускает.
 *
 * Python здесь НЕ стартует. `Application.onCreate` выполняется в каждом
 * процессе приложения, а интерпретатор нужен только одному — тому, где
 * живёт робот. Поднимать его в процессе экрана значило бы платить
 * секундами запуска и десятками мегабайт памяти за то, чем экран не
 * пользуется.
 */
class RobotApp : Application() {

    override fun onCreate() {
        super.onCreate()
        createChannel(this)
    }

    companion object {
        const val CHANNEL_ID = "cashmash.robot"

        /**
         * Канал создаётся в обоих процессах: уведомление показывает
         * служба, но обратиться к каналу может и экран.
         */
        fun createChannel(context: Context) {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
            val channel = NotificationChannel(
                CHANNEL_ID,
                context.getString(R.string.channel_name),
                // LOW, а не DEFAULT: уведомление здесь — не сообщение,
                // а признак жизни процесса. Звук при каждом запуске
                // приучил бы смахивать его не глядя.
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = context.getString(R.string.channel_description)
                setShowBadge(false)
            }
            context.getSystemService(NotificationManager::class.java)
                ?.createNotificationChannel(channel)
        }
    }
}
