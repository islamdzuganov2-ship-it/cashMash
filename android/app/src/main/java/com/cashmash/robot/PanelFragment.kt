package com.cashmash.robot

import android.annotation.SuppressLint
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.fragment.app.Fragment
import com.cashmash.robot.databinding.FragmentPanelBinding

/**
 * Вкладка «Панель» — та же страница, что на компьютере.
 *
 * Рисовать вторую панель средствами Android значило бы поддерживать две
 * картины одного состояния, и однажды они разошлись бы. Поэтому здесь
 * `WebView` и ничего больше, кроме честного объяснения, когда показывать
 * нечего: пустой белый экран — худший исход, по нему не понять, робот ли
 * стоит, порт ли занят или страница ещё грузится.
 *
 * Фрагмент НЕ пересоздаётся при переключении вкладок: экран прячет его,
 * а не выбрасывает. Иначе панель перезагружалась бы при каждом
 * возвращении, теряя прокрутку и полсекунды на разбор страницы.
 */
class PanelFragment : Fragment() {

    private var _binding: FragmentPanelBinding? = null
    private val binding get() = _binding!!
    private lateinit var prefs: Prefs
    private val ui = Handler(Looper.getMainLooper())

    private var loadedOnce = false

    private val poll = object : Runnable {
        override fun run() {
            render(Status.read(requireContext()))
            ui.postDelayed(this, 1_500)
        }
    }

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, saved: Bundle?
    ): View {
        _binding = FragmentPanelBinding.inflate(inflater, container, false)
        return binding.root
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onViewCreated(view: View, saved: Bundle?) {
        prefs = Prefs(requireContext())

        with(binding.panel.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            // Панель — локальная страница с котировками; кэш только мешает
            // отличить «данные устарели» от «страница из кэша».
            cacheMode = WebSettings.LOAD_NO_CACHE
            mediaPlaybackRequiresUserGesture = true
            allowFileAccess = false
            allowContentAccess = false
        }
        binding.panel.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView?, request: WebResourceRequest?
            ): Boolean {
                val url = request?.url ?: return false
                // Внутри остаётся только своя панель. Ссылка на новость
                // должна открываться в браузере: в окне без адресной
                // строки не видно, куда ты попал.
                if (url.host in setOf("127.0.0.1", "localhost")) return false
                startActivity(Intent(Intent.ACTION_VIEW, url))
                return true
            }
        }
        // Подсказка об отказе ведёт в журнал: причина лежит там, а сам
        // журнал во внутренней памяти — иначе до него не добраться.
        binding.hint.setOnClickListener {
            if (!Status.read(requireContext()).dashboardAlive) {
                (activity as? MainActivity)?.openTab(R.id.tab_logs)
            }
        }
    }

    /**
     * Опрос и работа панели идут, только пока вкладка открыта.
     *
     * Скрытая вкладка получает `onPause` — так устроен переход в
     * [MainActivity]. Без остановки скрипт внутри `WebView` продолжал бы
     * опрашивать робота раз в секунду за то, чего никто не видит.
     */
    override fun onResume() {
        super.onResume()
        binding.panel.onResume()
        binding.panel.resumeTimers()
        ui.post(poll)
    }

    override fun onPause() {
        ui.removeCallbacks(poll)
        binding.panel.onPause()
        binding.panel.pauseTimers()
        super.onPause()
    }

    override fun onDestroyView() {
        ui.removeCallbacks(poll)
        binding.panel.destroy()
        _binding = null
        super.onDestroyView()
    }

    /** Перезагрузить панель при следующем показе — например, после пуска. */
    fun forgetLoaded() {
        loadedOnce = false
    }

    private fun render(status: Status) {
        val b = _binding ?: return
        if (status.dashboardAlive) {
            b.hint.visibility = View.GONE
            b.panel.visibility = View.VISIBLE
            if (!loadedOnce) {
                loadedOnce = true
                b.panel.loadUrl(prefs.localUrl())
            }
            return
        }

        loadedOnce = false
        b.panel.visibility = View.GONE
        b.hint.visibility = View.VISIBLE
        b.hint.text = when {
            status.fatal.isNotEmpty() ->
                getString(R.string.hint_fatal, status.fatal)
            status.running ->
                getString(R.string.hint_dashboard_down, prefs.port)
            // Робота не пустила сеть. Причина обязана быть на экране, а
            // не только в уведомлении: человек нажал «Пуск» и смотрит сюда.
            RobotFiles.isNetworkPaused(requireContext())
                && !NetworkGate.allowed(requireContext(), prefs) ->
                getString(R.string.status_metered)
            else -> getString(R.string.hint_stopped)
        }
    }
}
