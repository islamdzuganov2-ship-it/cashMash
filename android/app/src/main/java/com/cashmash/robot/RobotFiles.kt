package com.cashmash.robot

import android.content.Context
import android.util.Log
import java.io.File

/**
 * Распаковка кода робота из APK в рабочий каталог.
 *
 * Зачем вообще распаковывать. Внутри APK файлы лежат в сжатом архиве, и
 * обычными файловыми вызовами их не открыть. Роботу же нужен настоящий
 * каталог: панель читает разметку и иконки как файлы, службы пишут
 * журналы и состояние рядом, конфиг правится руками. Проще один раз
 * разложить дерево на диск, чем учить пять служб читать ассеты.
 *
 * ЧТО ПЕРЕЖИВАЕТ ОБНОВЛЕНИЕ, А ЧТО НЕТ. Код заменяется целиком при каждой
 * новой сборке: он принадлежит приложению. Данные, конфиг и `ops/.env`
 * не трогаются никогда — они принадлежат человеку. Разделение проходит
 * по [USER_OWNED]: файл оттуда, если он уже есть на телефоне, из APK не
 * перезаписывается. Иначе обновление приложения молча стирало бы
 * настройку риска или токен Telegram, а заметили бы это по тому, что
 * робот перестал присылать сообщения.
 */
object RobotFiles {

    private const val TAG = "CashMashFiles"
    private const val STAMP = ".installed"

    /** Пути, которые принадлежат человеку, а не сборке. */
    private val USER_OWNED = listOf("config/", "ops/.env")

    /** Каталог робота. Внутренняя память приложения: её не видят другие
     *  программы, и она исчезает вместе с приложением — ключи биржи
     *  не должны лежать в общей папке «Загрузки». */
    fun root(context: Context): File = File(context.filesDir, "robot")

    fun dataDir(context: Context): File = File(root(context), "data")

    fun statusFile(context: Context): File = File(dataDir(context), "android_status.json")

    fun logsDir(context: Context): File = File(dataDir(context), "logs")

    fun envFile(context: Context): File = File(root(context), "ops/.env")

    /**
     * Признак «остановлен из-за тарифицируемой сети».
     *
     * Файл, а не настройка, и вот почему. Робот живёт в отдельном
     * процессе, а `SharedPreferences` между процессами НЕ
     * синхронизируются: каждый держит свой снимок в памяти и не
     * перечитывает файл. Флаг, выставленный роботом, экран просто не
     * увидел бы — и не поднял бы робота, когда вернулся Wi-Fi. Что и
     * случилось при проверке: сеть была безлимитной, робот стоял,
     * а приложение считало, что останавливать его было некому.
     *
     * Файл на диске читают оба процесса и оба видят одно и то же.
     */
    fun networkPauseFlag(context: Context): File =
        File(dataDir(context), "paused_by_network")

    fun setNetworkPause(context: Context, paused: Boolean) {
        val flag = networkPauseFlag(context)
        runCatching {
            if (paused) {
                flag.parentFile?.mkdirs()
                flag.writeText(System.currentTimeMillis().toString())
            } else {
                flag.delete()
            }
        }
    }

    fun isNetworkPaused(context: Context): Boolean = networkPauseFlag(context).isFile

    /**
     * Разложить дерево, если сборка изменилась.
     *
     * @return true, если распаковка выполнялась.
     */
    fun install(context: Context, force: Boolean = false): Boolean {
        val root = root(context)
        val stamp = File(root, STAMP)
        // Отпечаток содержимого дерева, посчитанный при сборке. По версии
        // приложения сверяться нельзя: правка в Python версию не меняет,
        // и распаковка её пропустила бы — робот остался бы на старом коде.
        val want = context.assets.open("project-stamp.txt")
            .bufferedReader().use { reader -> reader.readText().trim() }
        if (!force && stamp.isFile && stamp.readText() == want) return false

        val started = System.currentTimeMillis()
        root.mkdirs()
        val names = context.assets.open("project-files.txt")
            .bufferedReader().readLines().filter { it.isNotBlank() }

        var written = 0
        var kept = 0
        for (name in names) {
            val target = File(root, name)
            if (target.exists() && USER_OWNED.any { name.startsWith(it) }) {
                kept++
                continue
            }
            target.parentFile?.mkdirs()
            context.assets.open("project/$name").use { input ->
                target.outputStream().use { output -> input.copyTo(output) }
            }
            written++
        }

        // Отметка ставится последней: прерванная распаковка (батарея села,
        // система убила процесс) не должна выглядеть как успешная —
        // иначе робот запустится на половине файлов.
        stamp.writeText(want)
        dataDir(context).mkdirs()
        logsDir(context).mkdirs()

        val ms = System.currentTimeMillis() - started
        Log.i(TAG, "распаковано $written файлов, сохранено своих $kept, за $ms мс")
        return true
    }

    /**
     * Прочитать `ops/.env` в пары ключ-значение.
     *
     * Файл создаётся приложением и правится в настройках; формат тот же,
     * что на компьютере, чтобы его можно было перенести как есть.
     */
    fun readEnv(context: Context): Map<String, String> {
        val file = envFile(context)
        if (!file.isFile) return emptyMap()
        return file.readLines()
            .map { it.trim() }
            .filter { it.isNotEmpty() && !it.startsWith("#") && it.contains("=") }
            .associate { line ->
                val key = line.substringBefore("=").trim()
                val value = line.substringAfter("=").trim().trim('"', '\'')
                key to value
            }
    }

    /**
     * Дописать или изменить ключи в `ops/.env`, не трогая остальные.
     *
     * Пустое значение удаляет ключ: так «очистить токен» и «оставить как
     * было» остаются разными действиями.
     */
    fun writeEnv(context: Context, changes: Map<String, String>) {
        val merged = readEnv(context).toMutableMap()
        for ((key, value) in changes) {
            if (value.isBlank()) merged.remove(key) else merged[key] = value
        }
        val file = envFile(context)
        file.parentFile?.mkdirs()
        val body = buildString {
            appendLine("# Секреты робота. Создан приложением CashMash.")
            appendLine("# Файл лежит во внутренней памяти приложения и не попадает")
            appendLine("# ни в APK, ни в резервные копии.")
            for ((key, value) in merged.toSortedMap()) appendLine("$key=$value")
        }
        file.writeText(body)
    }
}
