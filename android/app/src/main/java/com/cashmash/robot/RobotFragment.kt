package com.cashmash.robot

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import com.cashmash.robot.databinding.FragmentRobotBinding
import com.cashmash.robot.databinding.ItemServiceBinding
import java.util.concurrent.TimeUnit

/**
 * Вкладка «Робот»: что происходит и почему.
 *
 * Раньше всё состояние умещалось в одну обрезанную строку вверху экрана
 * — «Работает служб: 3 · не поднялись: alerts». Из неё нельзя было
 * понять ни какая служба отказала, ни по какой причине, ни сколько раз
 * она перезапускалась. Данные для этого были всегда: супервизор пишет
 * их в `data/android_status.json` — не хватало места, где их показать.
 *
 * Здесь не дублируется панель. Панель отвечает на вопрос «что видит и
 * решает робот», эта вкладка — на вопрос «жив ли он и что ему мешает».
 */
class RobotFragment : Fragment() {

    private var _binding: FragmentRobotBinding? = null
    private val binding get() = _binding!!
    private lateinit var prefs: Prefs
    private val ui = Handler(Looper.getMainLooper())

    private val poll = object : Runnable {
        override fun run() {
            render()
            ui.postDelayed(this, 2_000)
        }
    }

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, saved: Bundle?
    ): View {
        _binding = FragmentRobotBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, saved: Bundle?) {
        prefs = Prefs(requireContext())
    }

    override fun onResume() {
        super.onResume()
        ui.post(poll)
    }

    override fun onPause() {
        ui.removeCallbacks(poll)
        super.onPause()
    }

    override fun onDestroyView() {
        ui.removeCallbacks(poll)
        _binding = null
        super.onDestroyView()
    }

    private fun render() {
        val b = _binding ?: return
        val context = context ?: return
        val status = Status.read(context)

        val metered = NetworkGate.isMetered(context)
        val waitingForNetwork = RobotFiles.isNetworkPaused(context)

        b.headline.text = when {
            status.running && status.dead.isEmpty() -> getString(R.string.robot_head_running)
            status.running -> getString(R.string.robot_head_partial)
            waitingForNetwork && metered -> getString(R.string.robot_head_waiting)
            else -> getString(R.string.robot_head_stopped)
        }
        b.headline.setTextColor(ContextCompat.getColor(context, when {
            status.running && status.dead.isEmpty() -> R.color.ok
            status.running || waitingForNetwork -> R.color.warn
            else -> R.color.text
        }))

        b.headnote.text = when {
            status.fatal.isNotEmpty() -> status.fatal
            status.running -> getString(R.string.robot_uptime,
                                        prefs.symbol, uptime(status))
            waitingForNetwork && metered -> getString(R.string.status_metered)
            else -> getString(R.string.robot_head_stopped_note)
        }

        renderServices(status)

        b.network.text = getString(
            if (metered) R.string.robot_network_metered
            else R.string.robot_network_free,
            if (prefs.unmeteredOnly) getString(R.string.robot_network_guarded)
            else getString(R.string.robot_network_unguarded))

        val used = status.rawBytes
        b.storage.text = if (prefs.rawLimitMb > 0)
            getString(R.string.robot_storage, mb(used), prefs.rawLimitMb)
        else getString(R.string.robot_storage_nolimit, mb(used))

        b.address.text = prefs.lanUrl()?.let { getString(R.string.robot_address_lan, it) }
            ?: getString(R.string.robot_address_local, prefs.port)
    }

    /**
     * Перерисовка списка службами, а не пересозданием всего экрана.
     *
     * Строки пересобираются целиком только когда изменился их набор: при
     * опросе раз в две секунды пересборка на каждом тике роняла бы
     * прокрутку под пальцем.
     */
    private fun renderServices(status: Status) {
        val b = _binding ?: return
        val context = context ?: return

        if (status.services.isEmpty()) {
            b.services.removeAllViews()
            b.servicesEmpty.visibility = View.VISIBLE
            return
        }
        b.servicesEmpty.visibility = View.GONE

        if (b.services.childCount != status.services.size) {
            b.services.removeAllViews()
            repeat(status.services.size) {
                b.services.addView(
                    ItemServiceBinding.inflate(layoutInflater, b.services, false).root)
            }
        }

        for ((i, service) in status.services.withIndex()) {
            val row = ItemServiceBinding.bind(b.services.getChildAt(i))
            row.name.text = service.name
            row.note.text = service.note

            val colour = when {
                service.alive -> R.color.ok
                service.stopped.isNotEmpty() -> R.color.warn
                // Робот просто выключен — это не авария. Красная точка
                // у каждой службы на остановленном роботе кричала бы
                // о поломке там, где её нет.
                !status.running -> R.color.text_dim
                else -> R.color.bad
            }
            row.dot.background.setTint(ContextCompat.getColor(context, colour))

            val reason = when {
                service.alive && service.restarts > 0 ->
                    getString(R.string.robot_restarts, service.restarts)
                service.alive -> ""
                service.stopped.isNotEmpty() -> humanize(service.stopped)
                service.error.isNotEmpty() -> humanize(service.error)
                else -> getString(R.string.robot_service_down)
            }
            row.reason.text = reason
            row.reason.visibility = if (reason.isEmpty()) View.GONE else View.VISIBLE
            row.reason.setTextColor(ContextCompat.getColor(
                context, if (status.running) colour else R.color.text_dim))
        }
    }

    /**
     * Убрать из причины внутренний путь.
     *
     * Службы писались для компьютера и честно называют файл, которого
     * не хватило: «не задан токен (ожидался в /data/user/0/…/ops/.env)».
     * На телефоне этот путь вреден вдвойне — он занимает две строки из
     * четырёх и советует пойти туда, куда без root не попасть. Токен
     * вписывается в настройках, и остаток фразы говорит именно это.
     *
     * Правится только показ. Полный текст, вместе с путём, лежит в
     * журнале — там он и нужен, когда разбираются всерьёз.
     */
    private fun humanize(reason: String): String = reason
        .replace(Regex("""\s*\([^)]*[/\\][^)]*\)"""), "")
        .replace(Regex("""\s+"""), " ")
        .trim()

    private fun uptime(status: Status): String {
        val ms = (status.tsMs - status.startedMs).coerceAtLeast(0)
        val hours = TimeUnit.MILLISECONDS.toHours(ms)
        val minutes = TimeUnit.MILLISECONDS.toMinutes(ms) % 60
        return if (hours > 0) getString(R.string.robot_hm, hours, minutes)
        else getString(R.string.robot_m, minutes)
    }

    private fun mb(bytes: Long): String =
        if (bytes < 1024 * 1024) "${bytes / 1024} КБ" else "${bytes / (1024 * 1024)} МБ"
}
