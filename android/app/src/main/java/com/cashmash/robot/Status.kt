package com.cashmash.robot

import android.content.Context
import org.json.JSONObject

/** Одна служба робота, как её видит супервизор. */
data class ServiceState(
    val name: String,
    val note: String,
    val alive: Boolean,
    val restarts: Int,
    /** Осознанный отказ: «не задан токен», «занята блокировка». */
    val stopped: String,
    /** Авария: исключение, с которым служба упала. */
    val error: String
)

/**
 * Состояние робота, прочитанное из файла супервизора.
 *
 * Читается ФАЙЛ, а не спрашивается процесс: робот живёт отдельным
 * процессом, которого может не быть вовсе, а файл есть всегда. Цена —
 * состояние отстаёт на секунды, и с этим приходится считаться:
 * см. [running].
 */
data class Status(
    val running: Boolean,
    val stopping: Boolean,
    val ageMs: Long,
    val tsMs: Long,
    val startedMs: Long,
    val rawBytes: Long,
    val fatal: String,
    val services: List<ServiceState>
) {
    val alive: List<String> get() = services.filter { it.alive }.map { it.name }

    val dead: List<String>
        get() = services.filter { !it.alive && it.stopped.isNotEmpty() }.map { it.name }

    /**
     * Живо ли то, что рисует панель.
     *
     * Обязательно И свежесть файла, И запись о службе. Файл остаётся на
     * диске после остановки, и последняя запись в нём застаёт потоки ещё
     * живыми — то есть говорит «dashboard работает» о роботе, которого
     * уже нет. Экран верил ей и показывал панель; панель поднимала из
     * кэша сервис-воркера свою оболочку, и это выглядело как работающий
     * робот. Хуже пустого экрана: пустой хотя бы честен.
     */
    val dashboardAlive: Boolean get() = running && "dashboard" in alive

    fun summary(context: Context): String = when {
        fatal.isNotEmpty() -> fatal
        !running -> context.getString(R.string.status_no_data)
        dead.isNotEmpty() ->
            context.getString(R.string.status_partial, alive.size, dead.joinToString(", "))
        else -> context.getString(R.string.status_running, alive.joinToString(", "))
    }

    companion object {
        /** Старше этого — уже не состояние, а воспоминание. */
        private const val FRESH_MS = 20_000L

        private val NOTHING = Status(
            running = false, stopping = false, ageMs = Long.MAX_VALUE,
            tsMs = 0, startedMs = 0, rawBytes = 0, fatal = "",
            services = emptyList())

        fun read(context: Context): Status {
            val file = RobotFiles.statusFile(context)
            if (!file.isFile) return NOTHING
            return try {
                val json = JSONObject(file.readText())
                val ts = json.optLong("ts_ms")
                val age = System.currentTimeMillis() - ts

                // Файл остаётся на диске и после смерти процесса.
                // Судить по его наличию значило бы показывать
                // работающим то, что не работает.
                val fresh = age in 0..FRESH_MS && !json.optBoolean("stopping")

                val list = json.optJSONArray("services")
                val services = buildList {
                    for (i in 0 until (list?.length() ?: 0)) {
                        val s = list!!.getJSONObject(i)
                        add(ServiceState(
                            name = s.optString("name"),
                            note = s.optString("note"),
                            // Свежесть учитывается ЗДЕСЬ, один раз на всех.
                            // Иначе выходит экран, спорящий сам с собой:
                            // заголовок «Остановлен», а точки служб зелёные.
                            // Так и было — устаревший снимок продолжал
                            // уверять, что три службы живы.
                            alive = fresh && s.optBoolean("alive"),
                            restarts = s.optInt("restarts"),
                            stopped = s.optString("stopped"),
                            error = s.optString("error")))
                    }
                }

                Status(
                    running = fresh,
                    stopping = json.optBoolean("stopping"),
                    ageMs = age,
                    tsMs = ts,
                    startedMs = json.optLong("started_ms"),
                    rawBytes = json.optLong("raw_bytes"),
                    fatal = json.optString("fatal"),
                    services = services)
            } catch (t: Throwable) {
                NOTHING
            }
        }
    }
}
