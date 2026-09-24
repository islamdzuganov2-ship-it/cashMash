package com.cashmash.robot

import android.content.ClipData
import android.content.ClipboardManager
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.CheckBox
import android.widget.LinearLayout
import android.widget.Toast
import androidx.fragment.app.Fragment
import com.cashmash.robot.databinding.FragmentSettingsBinding
import org.json.JSONArray
import java.io.File

/**
 * Вкладка «Настройки» — телефона, а не стратегии.
 *
 * Здесь выбирается, какие службы поднимать, на каком порту показывать
 * панель и куда слать сообщения. Параметры торговли — риск, издержки,
 * геометрия сделки — сюда не вынесены намеренно: они живут в YAML-конфиге
 * робота, проверяются его кодом и ограничены жёсткими потолками.
 * Ползунок «риск на сделку» в телефоне, который правится большим пальцем
 * в метро, — именно тот способ потерять счёт, от которого проект
 * защищается.
 */
class SettingsFragment : Fragment() {

    private var _binding: FragmentSettingsBinding? = null
    private val binding get() = _binding!!
    private lateinit var prefs: Prefs
    private val boxes = mutableMapOf<String, CheckBox>()

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, saved: Bundle?
    ): View {
        _binding = FragmentSettingsBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, saved: Bundle?) {
        prefs = Prefs(requireContext())

        binding.symbol.setText(prefs.symbol)
        binding.barSec.setText(prefs.barSec.toString())
        binding.port.setText(prefs.port.toString())
        binding.lan.isChecked = prefs.lanAccess
        binding.autostart.isChecked = prefs.autostart
        binding.unmeteredOnly.isChecked = prefs.unmeteredOnly
        binding.rawLimit.setText(prefs.rawLimitMb.toString())

        val env = RobotFiles.readEnv(requireContext())
        binding.tgToken.setText(env["CASHMASH_TG_TOKEN"].orEmpty())
        binding.tgChat.setText(env["CASHMASH_TG_CHAT_ID"].orEmpty())

        fillServices()
        fillLanAddress()
        binding.lan.setOnCheckedChangeListener { _, _ -> fillLanAddress() }
        binding.openLogs.setOnClickListener {
            (activity as? MainActivity)?.openTab(R.id.tab_logs)
        }
        binding.save.setOnClickListener { save() }
    }

    /** Занятое место могло измениться, пока вкладку не смотрели. */
    override fun onResume() {
        super.onResume()
        _binding?.rawNow?.text = getString(R.string.settings_raw_now, usedSpace())
    }

    override fun onDestroyView() {
        _binding = null
        super.onDestroyView()
    }

    /**
     * Показать адрес, по которому панель видна с другого устройства.
     *
     * Без этого опция «показывать панель в локальной сети» была
     * бесполезной: токен создавался, но увидеть его было негде, а без
     * него панель отвечает отказом. Включить функцию человек мог,
     * воспользоваться — нет.
     */
    private fun fillLanAddress() {
        val b = _binding ?: return
        val enabled = b.lan.isChecked
        val url = if (enabled) {
            prefs.lanAddress()?.let { ip -> "http://$ip:${prefs.port}/?t=${prefs.lanToken}" }
        } else null

        b.lanUrl.text = when {
            !enabled -> ""
            url == null -> getString(R.string.settings_lan_offline)
            else -> url
        }
        // Подпись прячется вместе с полем: заголовок без содержимого
        // выглядит как не загрузившееся значение.
        val visible = if (enabled) View.VISIBLE else View.GONE
        b.lanUrlLabel.visibility = visible
        b.lanUrl.visibility = visible
        b.lanCopy.visibility = visible
        b.lanRenew.visibility = visible
        b.lanCopy.isEnabled = url != null

        b.lanCopy.setOnClickListener {
            val clip = requireContext().getSystemService(ClipboardManager::class.java)
            clip?.setPrimaryClip(
                ClipData.newPlainText("CashMash", url ?: return@setOnClickListener))
            Toast.makeText(requireContext(), R.string.settings_lan_copied,
                           Toast.LENGTH_SHORT).show()
        }
        b.lanRenew.setOnClickListener {
            prefs.renewLanToken()
            fillLanAddress()
            Toast.makeText(requireContext(), R.string.settings_lan_renewed,
                           Toast.LENGTH_LONG).show()
        }
    }

    /** Сколько места уже занято записанными данными. */
    private fun usedSpace(): String {
        val raw = File(RobotFiles.dataDir(requireContext()), "raw")
        val bytes = raw.walkTopDown().filter { it.isFile }.sumOf { it.length() }
        return if (bytes < 1024 * 1024) "${bytes / 1024} КБ"
        else "${bytes / (1024 * 1024)} МБ"
    }

    /**
     * Список служб приходит из файла, который пишет супервизор.
     *
     * Дублировать список в Kotlin значило бы завести второй источник
     * правды: служба, добавленная в `ops/android_run.py`, не появилась бы
     * в настройках, и никто бы не понял, почему.
     *
     * Но и поднимать ради списка интерпретатор Python в процессе экрана
     * нельзя: это секунда ожидания и десятки мегабайт памяти за один
     * массив строк. Поэтому перечень выкладывает тот процесс, у которого
     * интерпретатор уже есть, а экран его просто читает. Цена — на
     * свежей установке, до первого запуска, списка ещё нет.
     */
    private fun fillServices() {
        val b = _binding ?: return
        val file = File(RobotFiles.dataDir(requireContext()), "android_catalog.json")
        val catalog = try {
            JSONArray(file.readText())
        } catch (t: Throwable) {
            b.servicesNote.text = getString(R.string.settings_services_unavailable)
            return
        }

        val chosen = prefs.services
        b.services.removeAllViews()
        boxes.clear()
        for (i in 0 until catalog.length()) {
            val item = catalog.getJSONObject(i)
            val name = item.optString("name")
            val heavy = item.optString("heavy")
            val box = CheckBox(requireContext()).apply {
                text = buildString {
                    append(name)
                    append(" — ")
                    append(item.optString("note"))
                    // Цена службы названа прямо в строке выбора, а не
                    // спрятана в справке: решение принимается здесь.
                    if (heavy.isNotEmpty()) append("\n⚠ $heavy")
                }
                isChecked = name in chosen
                layoutParams = LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT
                ).also { it.bottomMargin = 12 }
            }
            boxes[name] = box
            b.services.addView(box)
        }
    }

    private fun save() {
        val b = _binding ?: return
        prefs.symbol = b.symbol.text.toString().ifBlank { "XRPUSDT" }
        prefs.barSec = b.barSec.text.toString().toIntOrNull() ?: 15
        prefs.port = b.port.text.toString().toIntOrNull() ?: 8090
        prefs.lanAccess = b.lan.isChecked
        prefs.autostart = b.autostart.isChecked
        prefs.rawLimitMb = b.rawLimit.text.toString().toIntOrNull() ?: 2048
        prefs.unmeteredOnly = b.unmeteredOnly.isChecked
        if (boxes.isNotEmpty()) {
            prefs.services = boxes.filterValues { it.isChecked }.keys
        }

        RobotFiles.writeEnv(requireContext(), mapOf(
            "CASHMASH_TG_TOKEN" to b.tgToken.text.toString().trim(),
            "CASHMASH_TG_CHAT_ID" to b.tgChat.text.toString().trim()))

        // Настройки читаются при старте робота. Подменять их на ходу
        // нельзя: половина служб уже работает со старыми, и получилось
        // бы состояние, которого нет ни в одном конфиге.
        val running = Status.read(requireContext()).running
        Toast.makeText(
            requireContext(),
            if (running) R.string.settings_saved_restart else R.string.settings_saved,
            Toast.LENGTH_LONG
        ).show()
    }
}
