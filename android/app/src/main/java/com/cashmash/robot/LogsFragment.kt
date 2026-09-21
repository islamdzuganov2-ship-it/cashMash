package com.cashmash.robot

import android.content.Intent
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.AdapterView
import android.widget.ArrayAdapter
import androidx.core.content.FileProvider
import androidx.fragment.app.Fragment
import com.cashmash.robot.databinding.FragmentLogsBinding
import java.io.File
import java.io.RandomAccessFile

/**
 * Вкладка «Журнал».
 *
 * Зачем она нужна. Робот пишет в `data/logs/`, и сообщения об отказах
 * честно на эти файлы ссылаются — но лежат они во внутренней памяти
 * приложения. На телефоне без root туда не попасть ни файловым
 * менеджером, ни чем-либо ещё. То есть подсказка «смотрите
 * data/logs/dashboard.log» была советом, которому невозможно
 * последовать: причина есть, прочитать её нечем.
 *
 * Показывается ХВОСТ файла, а не файл целиком: журнал доходит до двух
 * мегабайт, а разбираются всегда с тем, что случилось только что.
 * Читается при этом тоже хвост, а не весь файл в память — отсюда
 * `RandomAccessFile` со смещением.
 */
class LogsFragment : Fragment() {

    private var _binding: FragmentLogsBinding? = null
    private val binding get() = _binding!!
    private var current: File? = null
    private var files: List<File> = emptyList()

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, saved: Bundle?
    ): View {
        _binding = FragmentLogsBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, saved: Bundle?) {
        binding.refresh.setOnClickListener { reload() }
        binding.share.setOnClickListener { share() }
    }

    /** Журналы перечитываются при каждом показе: робот мог дописать. */
    override fun onResume() {
        super.onResume()
        reload()
    }

    override fun onDestroyView() {
        _binding = null
        super.onDestroyView()
    }

    private fun reload() {
        val b = _binding ?: return
        val keep = current?.name
        files = collectLogs()

        if (files.isEmpty()) {
            b.body.text = getString(R.string.logs_empty)
            b.share.isEnabled = false
            b.picker.visibility = View.GONE
            return
        }
        b.picker.visibility = View.VISIBLE
        b.share.isEnabled = true

        b.picker.adapter = ArrayAdapter(
            requireContext(), android.R.layout.simple_spinner_dropdown_item,
            files.map { "${it.name} · ${it.length() / 1024} КБ" })
        b.picker.onItemSelectedListener =
            object : AdapterView.OnItemSelectedListener {
                override fun onItemSelected(
                    parent: AdapterView<*>?, view: View?, position: Int, id: Long
                ) = show(files[position])

                override fun onNothingSelected(parent: AdapterView<*>?) = Unit
            }

        // Выбор человека переживает обновление: иначе список каждый раз
        // прыгал бы на самый свежий файл, стоит роботу что-то записать.
        val index = files.indexOfFirst { it.name == keep }
        b.picker.setSelection(if (index >= 0) index else 0)
    }

    /** Журналы, самый свежий сверху: с него и начинают разбираться. */
    private fun collectLogs(): List<File> =
        RobotFiles.logsDir(requireContext()).listFiles()
            ?.filter { it.isFile && it.length() > 0 }
            ?.sortedByDescending { it.lastModified() }
            ?: emptyList()

    private fun show(file: File) {
        val b = _binding ?: return
        current = file
        b.body.text = tail(file, TAIL_BYTES)
        b.scroll.post { b.scroll.fullScroll(View.FOCUS_DOWN) }
    }

    private fun tail(file: File, limit: Long): String = try {
        RandomAccessFile(file, "r").use { raf ->
            val from = (raf.length() - limit).coerceAtLeast(0)
            raf.seek(from)
            val bytes = ByteArray((raf.length() - from).toInt())
            raf.readFully(bytes)
            val text = String(bytes, Charsets.UTF_8)
            // Срез по границе байтов может разрубить первую строку
            // пополам — и первое, что увидит человек, будет обрывком.
            val body = if (from > 0) text.substringAfter('\n', text) else text
            if (from > 0) getString(R.string.logs_truncated, from / 1024) + body else body
        }
    } catch (t: Throwable) {
        getString(R.string.logs_unreadable, t.message ?: t.javaClass.simpleName)
    }

    /**
     * Отдать файл наружу.
     *
     * Через FileProvider, а не текстом: журнал длиннее, чем принимает
     * буфер намерения, и обрезанный на середине он бесполезен.
     *
     * Отправляется он куда угодно, и это осознанное действие человека.
     * Токенов и ключей в журналах нет: секреты живут в `ops/.env`,
     * а в лог попадают только маски вида «токен …abcd».
     */
    private fun share() {
        val file = current ?: return
        val context = requireContext()
        val uri = FileProvider.getUriForFile(
            context, "${context.packageName}.logs", file)
        val intent = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_STREAM, uri)
            putExtra(Intent.EXTRA_SUBJECT, "CashMash · ${file.name}")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        startActivity(Intent.createChooser(intent, getString(R.string.logs_share)))
    }

    companion object {
        /** Хвост в 200 КБ: больше на экране телефона всё равно не читают. */
        private const val TAIL_BYTES = 200L * 1024
    }
}
