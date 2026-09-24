package com.cashmash.robot

import android.content.Context
import org.json.JSONObject

/**
 * Настройки приложения.
 *
 * Хранятся в SharedPreferences, а не в конфиге робота, и это осознанно:
 * YAML-конфиг в каталоге `config` описывает ТОРГОВЛЮ — риск, издержки,
 * геометрию сделки, — и проверяется кодом робота с жёсткими потолками.
 * Здесь же лежит только то, что относится к телефону: какие службы
 * поднимать, на каком порту показывать панель, просыпаться ли после
 * перезагрузки. Смешивать одно с другим нельзя: настройка «показывать
 * панель в сети» не должна жить рядом с «максимальным дневным убытком».
 *
 * ВАЖНО про процессы. Настройки читает и экран, и робот, а живут они
 * в разных процессах. SharedPreferences между процессами НЕ
 * синхронизируются: каждый процесс держит свой снимок в памяти. Поэтому
 * здесь лежит только то, что ПИШЕТ экран, а робот читает при запуске —
 * он стартует заново и файл перечитывает. Состояние, которое пишет
 * робот, а читает экран, хранится файлом (см. [RobotFiles]).
 */
class Prefs(context: Context) {

    private val sp = context.applicationContext
        .getSharedPreferences("cashmash", Context.MODE_PRIVATE)

    var symbol: String
        get() = sp.getString("symbol", "XRPUSDT") ?: "XRPUSDT"
        set(v) = sp.edit().putString("symbol", v.uppercase().trim()).apply()

    var barSec: Int
        get() = sp.getInt("bar_sec", 15)
        set(v) = sp.edit().putInt("bar_sec", v.coerceIn(1, 3600)).apply()

    var port: Int
        get() = sp.getInt("port", 8090)
        set(v) = sp.edit().putInt("port", v.coerceIn(1024, 65535)).apply()

    /** Видна ли панель другим устройствам в той же сети. */
    var lanAccess: Boolean
        get() = sp.getBoolean("lan", false)
        set(v) = sp.edit().putBoolean("lan", v).apply()

    /**
     * Токен доступа к панели снаружи.
     *
     * Создаётся приложением и живёт между запусками, а не рождается
     * заново в Python при каждом старте. Причина простая: ссылку с
     * токеном человек сохраняет в закладки, и меняющийся при каждом
     * перезапуске токен ломал бы её — а чинить это стали бы, выключив
     * проверку целиком.
     *
     * Сменить его можно явно: [renewLanToken].
     */
    var lanToken: String
        get() {
            val saved = sp.getString("lan_token", "").orEmpty()
            if (saved.isNotEmpty()) return saved
            return renewLanToken()
        }
        set(v) = sp.edit().putString("lan_token", v).apply()

    fun renewLanToken(): String {
        val bytes = ByteArray(18)
        java.security.SecureRandom().nextBytes(bytes)
        val token = android.util.Base64.encodeToString(
            bytes, android.util.Base64.URL_SAFE or android.util.Base64.NO_PADDING
                or android.util.Base64.NO_WRAP)
        lanToken = token
        return token
    }

    /** Полный адрес панели для другого устройства — или null, если выключено. */
    fun lanUrl(): String? {
        if (!lanAccess) return null
        val ip = lanAddress() ?: return null
        return "http://$ip:$port/?t=$lanToken"
    }

    /**
     * Адрес телефона в локальной сети.
     *
     * Берётся перечислением интерфейсов, а не у Wi-Fi-менеджера: тот
     * знает только про Wi-Fi и молчит, когда телефон раздаёт сеть сам
     * или подключён по USB.
     */
    fun lanAddress(): String? = runCatching {
        java.net.NetworkInterface.getNetworkInterfaces().toList()
            .filter { it.isUp && !it.isLoopback }
            .flatMap { it.inetAddresses.toList() }
            .firstOrNull { !it.isLoopbackAddress && it is java.net.Inet4Address }
            ?.hostAddress
    }.getOrNull()

    var autostart: Boolean
        get() = sp.getBoolean("autostart", false)
        set(v) = sp.edit().putBoolean("autostart", v).apply()

    /** Был ли робот запущен, когда процесс в прошлый раз закончился. */
    var wasRunning: Boolean
        get() = sp.getBoolean("was_running", false)
        set(v) = sp.edit().putBoolean("was_running", v).apply()

    var services: Set<String>
        get() = sp.getStringSet("services", DEFAULT_SERVICES) ?: DEFAULT_SERVICES
        set(v) = sp.edit().putStringSet("services", v).apply()

    /** Потолок на объём собранных данных, МБ. Ноль — не удалять ничего. */
    var rawLimitMb: Int
        get() = sp.getInt("raw_limit_mb", 2048)
        set(v) = sp.edit().putInt("raw_limit_mb", v.coerceAtLeast(0)).apply()

    /**
     * Работать только там, где трафик не считают.
     *
     * Включено по умолчанию, и это осознанный выбор умолчания в пользу
     * осторожности: робот держит открытым поток стакана, а счёт за
     * мобильный трафик приходит через месяц и ни с чем не связывается.
     * Выключить — одно касание; вернуть деньги — нет.
     */
    var unmeteredOnly: Boolean
        get() = sp.getBoolean("unmetered_only", true)
        set(v) = sp.edit().putBoolean("unmetered_only", v).apply()

    val host: String get() = if (lanAccess) "0.0.0.0" else "127.0.0.1"

    /** Адрес панели для встроенного экрана — всегда петлевой. */
    fun localUrl(): String = "http://127.0.0.1:$port/"

    /**
     * Настройки в том виде, в каком их ждёт `ops/android_run.py`.
     *
     * Имена полей совпадают с полями `Settings` в Python: лишнее там
     * отбрасывается, и добавить настройку можно, не трогая обе стороны
     * одновременно.
     */
    fun toJson(root: String): String = JSONObject().apply {
        put("root", root)
        put("symbol", symbol)
        put("bar_sec", barSec)
        put("port", port)
        put("host", host)
        put("raw_limit_mb", rawLimitMb)
        // Токен отдаётся только когда панель открыта наружу: на петлевом
        // адресе он лишний, а лишний секрет в файле состояния — это
        // секрет, который однажды окажется не в том файле.
        if (lanAccess) put("token", lanToken)
        put("services", org.json.JSONArray(services.toList()))
    }.toString()

    companion object {
        /** По умолчанию телефон наблюдает, но не пишет историю и не торгует. */
        val DEFAULT_SERVICES = setOf("dashboard", "paper", "news", "alerts")
    }
}
