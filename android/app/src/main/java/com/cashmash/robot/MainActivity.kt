package com.cashmash.robot

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.lifecycle.Lifecycle
import com.cashmash.robot.databinding.ActivityMainBinding

/**
 * Экран приложения: полоса управления, вкладка, навигация.
 *
 * ПОЧЕМУ ВКЛАДКИ, А НЕ ОДИН ЭКРАН. Сначала всё жило в одном: панель
 * робота во весь экран и строка состояния над ней. Панель отвечает на
 * вопрос «что робот видит и решает» — и отвечает хорошо. Но три других
 * вопроса ей не задать: жив ли процесс и что ему мешает, что он писал в
 * журнал, как он настроен. Они ютились по углам — состояние в одной
 * обрезанной строке, журнал и настройки за отдельными экранами, куда
 * вела единственная шестерёнка.
 *
 * Разделение идёт по РОДУ содержимого, а не по темам: состояние
 * процесса, картина рынка, диагностика, настройка. Это разные вопросы,
 * задаются они в разное время, и смешивать их на одном экране значит
 * мешать отвечать на каждый.
 *
 * ПОЧЕМУ ФРАГМЕНТЫ ЖИВУТ ОДНОВРЕМЕННО. Вкладки не пересоздаются, а
 * прячутся. Панель внутри `WebView` иначе перезагружалась бы при каждом
 * возвращении — полсекунды на разбор страницы и потерянная прокрутка.
 * Но спрятанная вкладка обязана перестать работать, поэтому ей
 * принудительно опускается жизненный цикл до `STARTED`: она получает
 * `onPause` и останавливает свой опрос. Без этого фоновая панель
 * продолжала бы дёргать робота раз в секунду.
 *
 * ПОЧЕМУ БЕЗ СМАХИВАНИЯ. Нижняя навигация и смахивание между экранами
 * не сочетаются: под пальцем оказываются то горизонтальная прокрутка
 * журнала, то содержимое панели, и жест угадывается неверно. Переход
 * делается нажатием и анимируется по направлению — влево или вправо
 * в зависимости от того, куда идёт человек.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: Prefs
    private val ui = Handler(Looper.getMainLooper())

    /** Порядок вкладок = порядок в меню. По нему считается направление. */
    private val tabs = listOf(
        R.id.tab_robot to "robot",
        R.id.tab_panel to "panel",
        R.id.tab_logs to "logs",
        R.id.tab_settings to "settings")

    private var currentTab = R.id.tab_robot

    private val askNotifications = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (!granted) {
            Toast.makeText(this, R.string.hint_no_notifications, Toast.LENGTH_LONG).show()
        }
    }

    private val poll = object : Runnable {
        override fun run() {
            renderTopBar()
            ui.postDelayed(this, 1_500)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        prefs = Prefs(this)

        if (savedInstanceState == null) addTabs()
        currentTab = savedInstanceState?.getInt(KEY_TAB) ?: R.id.tab_robot
        binding.nav.selectedItemId = currentTab
        showTab(currentTab, animate = false)

        binding.nav.setOnItemSelectedListener { item ->
            showTab(item.itemId, animate = true)
            true
        }
        // Повторное нажатие на активную вкладку не должно ничего делать:
        // иначе панель моргала бы анимацией на каждом промахе.
        binding.nav.setOnItemReselectedListener { }

        binding.power.setOnClickListener { togglePower() }

        askNotificationPermissionOnce()
        offerBatteryExemptionOnce()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putInt(KEY_TAB, currentTab)
    }

    override fun onResume() {
        super.onResume()
        ui.post(poll)
        resumeIfNetworkReturned()
    }

    override fun onPause() {
        ui.removeCallbacks(poll)
        super.onPause()
    }

    /** Назад с любой вкладки ведёт на первую, и лишь с неё — из приложения. */
    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        if (currentTab != R.id.tab_robot) {
            binding.nav.selectedItemId = R.id.tab_robot
            return
        }
        super.onBackPressed()
    }

    // --- вкладки ---------------------------------------------------------

    private fun addTabs() {
        supportFragmentManager.beginTransaction().apply {
            for ((id, tag) in tabs) {
                val fragment = create(id)
                add(binding.container.id, fragment, tag)
                hide(fragment)
                // Всё, кроме открытой вкладки, держится ниже RESUMED —
                // так спрятанный фрагмент получает onPause и замолкает.
                setMaxLifecycle(fragment, Lifecycle.State.STARTED)
            }
        }.commitNow()
    }

    private fun create(id: Int): Fragment = when (id) {
        R.id.tab_panel -> PanelFragment()
        R.id.tab_logs -> LogsFragment()
        R.id.tab_settings -> SettingsFragment()
        else -> RobotFragment()
    }

    /** Открыть вкладку извне — например, по ссылке «смотрите журнал». */
    fun openTab(id: Int) {
        binding.nav.selectedItemId = id
    }

    private fun showTab(id: Int, animate: Boolean) {
        val manager = supportFragmentManager
        val target = manager.findFragmentByTag(tagOf(id)) ?: return
        val previous = manager.findFragmentByTag(tagOf(currentTab))
        if (animate && target === previous) return

        val forward = indexOf(id) > indexOf(currentTab)
        manager.beginTransaction().apply {
            if (animate) {
                // Направление перехода — не украшение: оно говорит, куда
                // ты пошёл, и возврат ощущается возвратом.
                if (forward) setCustomAnimations(
                    R.anim.slide_in_right, R.anim.slide_out_left)
                else setCustomAnimations(
                    R.anim.slide_in_left, R.anim.slide_out_right)
            }
            if (previous != null && previous !== target) {
                hide(previous)
                setMaxLifecycle(previous, Lifecycle.State.STARTED)
            }
            show(target)
            setMaxLifecycle(target, Lifecycle.State.RESUMED)
        }.commit()

        currentTab = id
    }

    private fun tagOf(id: Int): String = tabs.first { it.first == id }.second

    private fun indexOf(id: Int): Int = tabs.indexOfFirst { it.first == id }

    // --- полоса управления -----------------------------------------------

    private fun renderTopBar() {
        val status = Status.read(this)
        val waiting = RobotFiles.isNetworkPaused(this) && NetworkGate.isMetered(this)

        binding.power.text = getString(
            if (status.running) R.string.action_stop else R.string.action_start)

        binding.state.text = when {
            status.running && status.dead.isEmpty() -> getString(R.string.robot_head_running)
            status.running -> getString(R.string.robot_head_partial)
            waiting -> getString(R.string.robot_head_waiting)
            else -> getString(R.string.robot_head_stopped)
        }
        binding.substate.text = when {
            status.fatal.isNotEmpty() -> status.fatal
            status.running -> status.alive.joinToString(", ")
            waiting -> getString(R.string.robot_waiting_short)
            else -> getString(R.string.robot_tap_start)
        }

        val colour = when {
            status.running && status.dead.isEmpty() -> R.color.ok
            status.running || waiting -> R.color.warn
            else -> R.color.bad
        }
        binding.dot.background.setTint(ContextCompat.getColor(this, colour))
    }

    private fun togglePower() {
        if (Status.read(this).running) {
            BotService.stop(this)
        } else {
            (supportFragmentManager.findFragmentByTag("panel") as? PanelFragment)
                ?.forgetLoaded()
            BotService.start(this)
        }
        ui.postDelayed({ renderTopBar() }, 400)
    }

    /**
     * Поднять робота, если он был остановлен из-за сотовой сети, а
     * телефон уже вернулся в безлимитную.
     *
     * Делается именно здесь, а не в фоне: Android запрещает фоновым
     * приложениям поднимать службы переднего плана, и «самостоятельное
     * возвращение по появлению Wi-Fi» упёрлось бы в этот запрет —
     * причём молча. Когда приложение открыто, запрета нет, и подъём
     * срабатывает всегда. Цена честная: робот вернётся не в тот миг,
     * когда появился Wi-Fi, а когда на него посмотрят.
     */
    private fun resumeIfNetworkReturned() {
        if (!RobotFiles.isNetworkPaused(this)) return
        if (Status.read(this).running) return
        if (!NetworkGate.allowed(this, prefs)) return

        RobotFiles.setNetworkPause(this, false)
        (supportFragmentManager.findFragmentByTag("panel") as? PanelFragment)
            ?.forgetLoaded()
        BotService.start(this)
        Toast.makeText(this, R.string.status_resumed, Toast.LENGTH_LONG).show()
    }

    // --- разрешения ------------------------------------------------------

    private fun askNotificationPermissionOnce() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.POST_NOTIFICATIONS
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    /**
     * Предложить исключение из экономии батареи — один раз.
     *
     * Без него Android усыпляет процесс в Doze, и робот просыпается
     * с разорванной связью, пропустив часть ленты. Системный диалог
     * запроса вызывать нельзя без веской причины, поэтому открываются
     * настройки — решение остаётся за человеком.
     */
    private fun offerBatteryExemptionOnce() {
        val sp = getSharedPreferences("cashmash", MODE_PRIVATE)
        if (sp.getBoolean("battery_offered", false)) return
        val pm = getSystemService(PowerManager::class.java)
        if (pm.isIgnoringBatteryOptimizations(packageName)) return

        // Отметка ставится при ПОКАЗЕ, а не при нажатии кнопки. Иначе
        // диалог, закрытый касанием мимо него, считается непоказанным и
        // возвращается при каждом запуске — а первое касание по экрану
        // после старта попадает уже не туда, куда целился человек.
        sp.edit().putBoolean("battery_offered", true).apply()

        AlertDialog.Builder(this)
            .setTitle(R.string.battery_title)
            .setMessage(R.string.battery_message)
            .setPositiveButton(R.string.battery_open) { _, _ ->
                runCatching {
                    startActivity(Intent(
                        Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
                }.onFailure {
                    startActivity(Intent(Settings.ACTION_SETTINGS))
                }
            }
            .setNegativeButton(R.string.battery_later, null)
            .show()
    }

    companion object {
        private const val KEY_TAB = "tab"
    }
}
