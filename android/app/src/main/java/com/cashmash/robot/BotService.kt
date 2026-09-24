package com.cashmash.robot

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlin.system.exitProcess

/**
 * Процесс робота.
 *
 * Служба переднего плана — единственный способ держать на Android
 * долгоживущую работу. Система гарантирует такому процессу жизнь, пока
 * висит уведомление, и убивает всё остальное, когда кончается память.
 * Поэтому уведомление здесь не украшение, а условие существования
 * робота; скрыть его нельзя.
 *
 * Внутри процесса: интерпретатор Python, а в нём — супервизор
 * `ops/android_run.py`, который поднимает службы потоками. Отсюда в
 * Python уходит три вызова (`start`, `stop`, `status`) и ни одного
 * больше: вся логика остаётся на стороне робота.
 */
class BotService : Service() {

    private lateinit var prefs: Prefs
    private var wakeLock: PowerManager.WakeLock? = null
    private var worker: Thread? = null
    private var networkWatch: NetworkGate.Watch? = null
    private val ui = Handler(Looper.getMainLooper())

    @Volatile private var stopping = false
    @Volatile private var lastLine = ""

    private val ticker = object : Runnable {
        override fun run() {
            if (stopping) return
            refreshNotification()
            ui.postDelayed(this, STATUS_PERIOD_MS)
        }
    }

    override fun onBind(intent: Intent?) = null

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        RobotApp.createChannel(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Уведомление — первым делом и до любой работы: на постановку
        // службы переднего плана система даёт несколько секунд, а
        // распаковка файлов в них может не уложиться.
        ServiceCompat.startForeground(
            this, NOTIFICATION_ID, buildNotification(getString(R.string.status_starting)),
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE)
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE else 0
        )

        when (intent?.action) {
            ACTION_STOP -> {
                stopRobot(getString(R.string.status_stopped_by_user))
                return START_NOT_STICKY
            }
            else -> startRobot()
        }
        // START_STICKY: если системе всё же пришлось убить процесс
        // (например, кончилась память), она поднимет службу снова.
        return START_STICKY
    }

    private fun startRobot() {
        if (worker?.isAlive == true) return

        // Отказ ДО запуска интерпретатора: поднять Python, открыть поток
        // и тут же его оборвать — значит потратить тот самый трафик,
        // ради которого проверка и сделана.
        if (!NetworkGate.allowed(this, prefs)) {
            RobotFiles.setNetworkPause(this, true)
            prefs.wasRunning = false
            finishWith(getString(R.string.status_metered))
            return
        }

        stopping = false
        prefs.wasRunning = true
        RobotFiles.setNetworkPause(this, false)
        acquireWakeLock()
        watchNetwork()
        ui.post(ticker)

        worker = Thread({
            val root = RobotFiles.root(this)
            try {
                RobotFiles.install(this)

                if (!Python.isStarted()) Python.start(AndroidPlatform(this))
                val py = Python.getInstance()
                val bridge = py.getModule("cashmash_android")

                Log.i(TAG, "робот стартует в ${root.absolutePath}")
                // Вызов блокирует поток до остановки робота.
                val result = bridge.callAttr("start", prefs.toJson(root.absolutePath))
                    .toString()
                Log.i(TAG, "робот завершился: $result")
                if (!stopping) {
                    // Робот кончился сам, а его об этом не просили.
                    // Молча убрать уведомление было бы худшим исходом:
                    // человек считал бы, что всё работает.
                    finishWith(
                        if (result.startsWith("error:"))
                            getString(R.string.status_failed, result.removePrefix("error:").trim())
                        else getString(R.string.status_finished)
                    )
                }
            } catch (t: Throwable) {
                Log.e(TAG, "робот не запустился", t)
                if (!stopping) finishWith(getString(R.string.status_failed, t.message ?: t.javaClass.simpleName))
            }
        }, "cashmash-python").also { it.start() }
    }

    /**
     * Остановка.
     *
     * Потоки служб внутри Python прервать нечем: ни одна из них не
     * проверяет флаг остановки — на компьютере их останавливает смерть
     * процесса, и переписывать пять служб ради телефона было бы
     * неправильно. Поэтому порядок такой: попросить Python отпустить
     * захваченное (блокировку сборщика), дать короткую паузу на сброс
     * буферов и погасить процесс.
     *
     * Потеря при этом ограничена: сборщик сбрасывает данные на диск
     * каждые 5 секунд и переживает жёсткое завершение — так он задуман.
     */
    private fun stopRobot(reason: String) {
        if (stopping) return
        stopping = true
        prefs.wasRunning = false
        ui.removeCallbacks(ticker)
        updateNotification(getString(R.string.status_stopping))

        Thread({
            try {
                if (Python.isStarted()) {
                    Python.getInstance().getModule("cashmash_android")
                        .callAttr("stop", reason)
                }
            } catch (t: Throwable) {
                Log.w(TAG, "остановка Python прошла не гладко", t)
            }
            Thread.sleep(FLUSH_PAUSE_MS)
            releaseWakeLock()
            networkWatch?.cancel()
            networkWatch = null
            ui.post {
                ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
                stopSelf()
                // Процесс гасится целиком: потоки служб — демоны внутри
                // живой виртуальной машины, и сами они не закончатся.
                // Процесс отдельный (`:bot`), поэтому экран приложения
                // от этого не страдает.
                exitProcess(0)
            }
        }, "cashmash-stop").start()
    }

    /**
     * Остановиться, если сеть стала тарифицируемой.
     *
     * Телефон переходит с Wi-Fi на сотовую сам — в лифте, в дороге, при
     * выходе из дома. Без этой проверки робот продолжил бы качать
     * стакан за деньги, ничего не сообщив.
     *
     * Возобновление — не здесь. Android запрещает фоновым приложениям
     * поднимать службы переднего плана, и попытка «подняться самому,
     * когда вернётся Wi-Fi» упёрлась бы в этот запрет и молча не
     * сработала. Поэтому робота поднимает экран приложения, когда его
     * открывают уже в безлимитной сети, — это работает всегда.
     */
    private fun watchNetwork() {
        if (!prefs.unmeteredOnly) return
        networkWatch?.cancel()
        networkWatch = NetworkGate.watch(this) {
            if (stopping) return@watch
            Log.i(TAG, "сеть стала тарифицируемой — останавливаю робота")
            RobotFiles.setNetworkPause(this, true)
            ui.post { stopRobot(getString(R.string.status_metered_stop)) }
        }
    }

    /** Робот кончился не по команде: оставить причину на экране. */
    private fun finishWith(message: String) {
        stopping = true
        prefs.wasRunning = false
        ui.post {
            ui.removeCallbacks(ticker)
            networkWatch?.cancel()
            networkWatch = null
            releaseWakeLock()
            ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_DETACH)
            updateNotification(message, ongoing = false)
            stopSelf()
        }
    }

    // --- уведомление ---------------------------------------------------

    private fun buildNotification(text: String, ongoing: Boolean = true): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, BotService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return NotificationCompat.Builder(this, RobotApp.CHANNEL_ID)
            .setContentTitle(getString(R.string.notification_title, prefs.symbol))
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setSmallIcon(R.drawable.ic_stat_robot)
            .setContentIntent(open)
            .setOngoing(ongoing)
            .setSilent(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .apply {
                if (ongoing) addAction(0, getString(R.string.action_stop), stop)
            }
            .build()
    }

    private fun updateNotification(text: String, ongoing: Boolean = true) {
        androidx.core.app.NotificationManagerCompat.from(this)
            .let { manager ->
                try {
                    manager.notify(NOTIFICATION_ID, buildNotification(text, ongoing))
                } catch (_: SecurityException) {
                    // Разрешения на уведомления нет. Робот при этом
                    // работает; экран покажет состояние сам.
                }
            }
    }

    /**
     * Что написать в уведомлении.
     *
     * Состояние берётся из файла, который пишет супервизор, а не
     * спрашивается у Python через мост: файл уже есть, его читает и
     * экран, и лишний вызов через JNI каждые несколько секунд ничего
     * не добавляет.
     */
    private fun refreshNotification() {
        val line = Status.read(this).summary(this)
        if (line != lastLine) {
            lastLine = line
            updateNotification(line)
        }
    }

    // --- удержание от сна ------------------------------------------------

    private fun acquireWakeLock() {
        if (wakeLock != null) return
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "cashmash:robot").apply {
            setReferenceCounted(false)
            // Без таймаута: смысл в том и состоит, чтобы работать, пока
            // человек не остановит. Отпускается в stopRobot и onDestroy.
            acquire()
        }
    }

    private fun releaseWakeLock() {
        try {
            wakeLock?.takeIf { it.isHeld }?.release()
        } catch (t: Throwable) {
            Log.w(TAG, "wake lock уже отпущен", t)
        }
        wakeLock = null
    }

    override fun onDestroy() {
        ui.removeCallbacks(ticker)
        networkWatch?.cancel()
        networkWatch = null
        releaseWakeLock()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "CashMashBot"
        private const val NOTIFICATION_ID = 1
        private const val STATUS_PERIOD_MS = 5_000L
        private const val FLUSH_PAUSE_MS = 1_200L

        const val ACTION_START = "com.cashmash.robot.START"
        const val ACTION_STOP = "com.cashmash.robot.STOP"

        fun start(context: Context) {
            val intent = Intent(context, BotService::class.java).setAction(ACTION_START)
            context.startForegroundService(intent)
        }

        fun stop(context: Context) {
            val intent = Intent(context, BotService::class.java).setAction(ACTION_STOP)
            context.startService(intent)
        }
    }
}
