/* CashMash — клиент панели.
 *
 * Тонкий по построению: умеет только рисовать то, что отдал сервер.
 * Ни одного торгового решения здесь не принимается — панель может
 * упасть как угодно, торговля не заметит. Отправляет она ровно одно:
 * ключ биржи, введённый человеком в карточке «Подключение биржи».
 *
 * Два режима чтения. «Новичок» прячет элементы с классом .pro (сырые
 * bps, веса, идентификаторы) и оставляет объяснения словами. «Профи»
 * показывает всё. Переключение не меняет данные — только плотность.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const fmt = (x, n = 2) => (x === null || x === undefined || Number.isNaN(x))
  ? "—" : Number(x).toFixed(n);
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
/* Экранирование для всего, что пришло не от нас: заголовки новостей,
   отказы сервера, текст отчёта аналитика. Стоит здесь, наверху, рядом
   с остальными помощниками — им пользуются несколько карточек, и
   вторая такая же функция ниже по файлу однажды уже сломала панель
   целиком: `const` не терпит повторного объявления, и на этом
   останавливается весь скрипт, а не одна карточка. */
const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ── темы и режим чтения ─────────────────────────────────────── */
const saved = (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } };
const store = (k, v) => { try { localStorage.setItem(k, v); } catch { /* приватный режим */ } };

let level = saved("cm-level", "beginner");
function applyLevel() {
  document.body.classList.toggle("beginner", level === "beginner");
  $("level").textContent = "Режим: " + (level === "beginner" ? "новичок" : "профи");
}
$("level").onclick = () => {
  level = level === "beginner" ? "pro" : "beginner";
  store("cm-level", level); applyLevel();
};
applyLevel();

$("theme").onclick = () => {
  const cur = document.documentElement.getAttribute("data-theme");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  store("cm-theme", next);
};
const t0 = saved("cm-theme", null);
if (t0) document.documentElement.setAttribute("data-theme", t0);

/* ── вкладки ───────────────────────────────────────────────────
 *
 * Три вкладки отвечают на три разных вопроса: что на рынке, что с
 * деньгами и что показала проверка. Выбранная запоминается — панель
 * открывают десятки раз в день, и начинать каждый раз с аналитики,
 * когда следишь за сделками, значит делать лишнее движение.
 *
 * Скрытые вкладки продолжают перерисовываться: карточки дешёвые, а
 * рисовать только видимое означало бы завести второй источник правды
 * о том, что сейчас на экране. Исключение — график: он меряет свою
 * ширину, а у скрытого элемента она нулевая. Поэтому при возврате на
 * вкладку панель перерисовывает последний снимок сразу, не дожидаясь
 * следующего опроса, — иначе секунду висел бы график не той ширины.
 */
let tab = saved("cm-tab", "analytics");

function applyTab() {
  document.querySelectorAll(".tab").forEach(
    (n) => n.classList.toggle("on", n.id === "tab-" + tab));
  document.querySelectorAll("#seg button").forEach(
    (b) => b.classList.toggle("on", b.dataset.tab === tab));
}
document.querySelectorAll("#seg button").forEach((b) => {
  b.onclick = () => {
    tab = b.dataset.tab;
    store("cm-tab", tab);
    applyTab();
    if (snap) paint(snap);
    window.scrollTo({ top: 0 });
  };
});
applyTab();

/* ── строка состояния ────────────────────────────────────────── */
function renderStatus(s) {
  const hb = s.heartbeat, br = s.brain;
  const live = hb && (s.now_ms - (hb.ts_ms || 0) < 30000);

  let colour = "var(--ink-muted)", head = "Данных нет", why = "";
  if (!live) {
    colour = "var(--critical)";
    head = "Сбор данных не идёт";
    // Совет зависит от того, ГДЕ работает робот, а не откуда смотрят:
    // панель телефона открывают и с компьютера, и наоборот. Поэтому
    // площадку сообщает сервер, а не угадывает браузер.
    why = s.platform === "android"
        ? "Сборщик не подаёт признаков жизни. Без потока робот слеп — "
          + "включите «collector» в настройках приложения."
        : "Сборщик не подаёт признаков жизни. Без потока робот слеп — запустите ops\\collect.cmd";
  } else if (br && br.would_enter) {
    colour = "var(--good)";
    head = "Условия для сделки сошлись";
    why = "Все проверки пройдены. Торговый процесс войдёт, если запущен и есть ключи.";
  } else if (br && br.blocker) {
    colour = "var(--warning)";
    head = "Робот наблюдает и не торгует";
    why = "Не хватает одного: " + br.blocker.label.toLowerCase()
        + " (" + br.blocker.value + ").";
  } else {
    colour = "var(--accent)";
    head = "Поток идёт";
    why = "Решение ещё не посчитано.";
  }
  $("dot").style.background = colour;
  $("verdict").textContent = head;
  $("verdict-why").textContent = why;

  const chips = [];
  const chip = (mark, label, value, tone) =>
    `<span class="chip"><span class="mark" style="color:${tone}">${mark}</span>` +
    `${label} <b>${value}</b></span>`;

  if (hb) {
    const age = Math.round((s.now_ms - (hb.ts_ms || 0)) / 1000);
    chips.push(chip(live ? "●" : "▲", "сборщик",
      live ? `${(hb.uptime_sec / 3600).toFixed(1)} ч` : `молчит ${age} с`,
      live ? "var(--good)" : "var(--critical)"));
    chips.push(chip(hb.in_sync ? "●" : "▲", "стакан",
      hb.in_sync ? "синхронен" : "рассинхрон",
      hb.in_sync ? "var(--good)" : "var(--critical)"));
    chips.push(chip(hb.data_age_ms < 1000 ? "●" : "▲", "задержка",
      `${hb.data_age_ms} мс`,
      hb.data_age_ms < 1000 ? "var(--good)" : "var(--warning)"));
    chips.push(chip(hb.gaps ? "▲" : "●", "разрывы", hb.gaps,
      hb.gaps ? "var(--warning)" : "var(--good)"));
    chips.push(`<span class="chip pro">сделок <b>${hb.trades}</b></span>`);
    chips.push(`<span class="chip pro">снимков <b>${hb.snapshots}</b></span>`);
  }
  if (br) {
    const c = br.counters;
    chips.push(chip(c.bars >= c.warmup_need ? "●" : "◐", "прогрев",
      `${Math.min(c.bars, c.warmup_need)}/${c.warmup_need}`,
      c.bars >= c.warmup_need ? "var(--good)" : "var(--warning)"));
  }
  if (br) {
    const off = br.counters.clock_offset_ms || 0;
    chips.push(chip(Math.abs(off) <= 1000 ? "●" : "▲", "часы",
      `${off >= 0 ? "+" : ""}${off} мс`,
      Math.abs(off) <= 1000 ? "var(--good)" : "var(--warning)"));
  }
  const bot = s.bot || {};
  chips.push(chip(bot.alive ? "●" : "○", "торговый процесс",
    bot.alive ? (bot.heartbeat?.mode || "работает") : "не запущен",
    bot.alive ? "var(--good)" : "var(--ink-muted)"));
  $("chips").innerHTML = chips.join("");
}

/* ── цена и спарклайн ────────────────────────────────────────── */
function renderPrice(s) {
  const mid = s.mid || [];
  if (!mid.length) { $("price").textContent = "—"; return; }
  const last = mid[mid.length - 1], first = mid[0];
  // Середина бида и аска даёт лишний разряд; хвостовой ноль в цене
  // читается как значащая цифра, которой нет.
  $("price").textContent = last.m.toFixed(5).replace(/0+$/, "").replace(/\.$/, "");

  const chgBps = (last.m - first.m) / first.m * 10000;
  const el = $("chg");
  el.textContent = (chgBps >= 0 ? "▲ " : "▼ ") + fmt(Math.abs(chgBps), 1) + " bps"
    + (level === "beginner" ? "" : " за окно");
  el.className = "delta " + (chgBps >= 0 ? "up" : "down");

  const sp = last.s;
  const verdict = sp < 1 ? "узкий — вход дешёвый"
    : sp < 3 ? "обычный" : "широкий — вход дорогой";
  $("spread").textContent = `спред ${fmt(sp, 2)} bps · ${verdict}`;

  drawSpark(mid, (s.brain || {}).plan, s.paper);
}

/* ── график цены ─────────────────────────────────────────────────────
 *
 * Не спарклайн, а полноценный график: без осей число на экране нельзя
 * ни к чему привязать — непонятно, за какое время движение и насколько
 * далеко уровни сделки от текущей цены. Поэтому здесь есть шкала цены
 * слева, шкала времени снизу и горизонтали плана: вход, стоп, цель.
 *
 * Область значений РАСШИРЯЕТСЯ до уровней плана. Иначе стоп в 20 bps
 * просто не попадает в кадр при размахе цены в 5 bps, и самая нужная
 * часть картинки оказывается за краем.
 */
const CH = { w: 900, h: 280, l: 60, r: 78, t: 12, b: 28 };

function niceTicks(lo, hi, count) {
  const span = (hi - lo) || 1;
  const raw = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(v => v >= raw) || mag * 10;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-12; v += step) out.push(v);
  return out;
}

function drawSpark(mid, plan, paper) {
  const wpx = $("spark").getBoundingClientRect().width || 900;
  const px = (i) => CH.l + (i / Math.max(1, mid.length - 1)) * (CH.w - CH.l - CH.r);
  const vals = mid.map(d => d.m);
  let lo = Math.min(...vals), hi = Math.max(...vals);

  const levels = [];
  const pos = paper && paper.alive ? paper.position : null;
  if (pos) {
    levels.push({ v: +pos.entry, c: "var(--accent)", t: "вход" });
    levels.push({ v: +pos.sl, c: "var(--critical)", t: "стоп" });
    levels.push({ v: +pos.tp, c: "var(--good)", t: "цель" });
  } else if (plan && plan.symmetric) {
    // Стороны нет: показываем МАСШТАБ, а не направление.
    levels.push({ v: plan.tp, c: "var(--good)", t: "цель +", dash: true });
    levels.push({ v: plan.sl_up, c: "var(--critical)", t: "стоп +", dash: true });
    levels.push({ v: plan.sl, c: "var(--critical)", t: "стоп −", dash: true });
    levels.push({ v: plan.tp_down, c: "var(--good)", t: "цель −", dash: true });
  } else if (plan) {
    levels.push({ v: plan.entry, c: "var(--accent)", t: "вход", dash: true });
    levels.push({ v: plan.sl, c: "var(--critical)", t: "стоп", dash: true });
    levels.push({ v: plan.tp, c: "var(--good)", t: "цель", dash: true });
  }
  for (const L of levels) { lo = Math.min(lo, L.v); hi = Math.max(hi, L.v); }

  const pad = ((hi - lo) || lo * 1e-4) * 0.06;
  lo -= pad; hi += pad;
  const py = (v) => CH.t + (1 - (v - lo) / (hi - lo)) * (CH.h - CH.t - CH.b);

  // Число знаков ОГРАНИЧЕНО сверху: середина бида и аска — результат
  // деления, и её представление может дать шестнадцать знаков.
  // toFixed(16) выдаёт подпись, которая не помещается на оси и
  // вырождается в бессмысленную строку из нулей.
  const digits = Math.min(6, Math.max(4,
    String(mid[0].m).split(".")[1]?.length ?? 4));
  const yticks = niceTicks(lo, hi, 5).map(v =>
    `<line class="gridline" x1="${CH.l}" x2="${CH.w - CH.r}"
           y1="${py(v).toFixed(1)}" y2="${py(v).toFixed(1)}"/>
     <text class="ax" x="${CH.l - 8}" y="${(py(v) + 3.5).toFixed(1)}"
           text-anchor="end">${v.toFixed(digits)}</text>`).join("");

  // Число подписей времени — от ФАКТИЧЕСКОЙ ширины холста, а не
  // фиксированное: пять меток, удобные на десктопе, на телефоне
  // налезают друг на друга и перестают читаться совсем.
  const cnt = wpx < 420 ? 3 : wpx < 700 ? 4 : 5;
  const secs = wpx < 420 ? {} : { second: "2-digit" };
  const n = mid.length, marks = [];
  for (let k = 0; k < cnt; k++) {
    const i = Math.round(k * (n - 1) / (cnt - 1));
    const tm = new Date(mid[i].t).toLocaleTimeString("ru-RU",
      { hour: "2-digit", minute: "2-digit", ...secs });
    marks.push(`<text class="ax" x="${px(i).toFixed(1)}" y="${CH.h - 8}"
      text-anchor="${k === 0 ? "start" : k === cnt - 1 ? "end" : "middle"}">${tm}</text>`);
  }

  const pts = mid.map((d, i) => `${px(i).toFixed(1)},${py(d.m).toFixed(1)}`).join(" ");
  const area = `M${px(0).toFixed(1)},${CH.h - CH.b} L`
    + pts.replace(/ /g, " L") + ` L${px(n - 1).toFixed(1)},${CH.h - CH.b} Z`;

  const lvlSvg = levels.map(L => `
    <line x1="${CH.l}" x2="${CH.w - CH.r}" y1="${py(L.v).toFixed(1)}"
          y2="${py(L.v).toFixed(1)}" stroke="${L.c}" stroke-width="1.5"
          ${L.dash ? 'stroke-dasharray="5 4"' : ""} opacity="0.9"/>
    <text class="lvl-label" fill="${L.c}" x="${CH.w - CH.r + 6}"
          y="${(py(L.v) + 3.5).toFixed(1)}">${wpx < 520 ? L.t
            : L.t + " " + L.v.toFixed(digits)}</text>`
  ).join("");

  $("sparksvg").innerHTML = `
    <defs><linearGradient id="g" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0" stop-color="var(--accent)" stop-opacity="0.16"/>
      <stop offset="1" stop-color="var(--accent)" stop-opacity="0"/>
    </linearGradient></defs>
    ${yticks}${marks.join("")}
    <line class="axisline" x1="${CH.l}" x2="${CH.w - CH.r}"
          y1="${CH.h - CH.b}" y2="${CH.h - CH.b}"/>
    <path d="${area}" fill="url(#g)"/>
    <polyline points="${pts}" fill="none" stroke="var(--accent)"
              stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    ${lvlSvg}
    <line id="cx" x1="0" y1="${CH.t}" x2="0" y2="${CH.h - CH.b}"
          stroke="var(--baseline)" stroke-width="1" opacity="0"/>
    <circle id="cdot" r="4" fill="var(--accent)" stroke="var(--surface)"
            stroke-width="2" opacity="0"/>`;

  const sym = plan && plan.symmetric;
  $("legend").innerHTML = !levels.length
    ? `<span style="color:var(--ink-muted)">Уровни появятся, когда пойдёт поток.</span>`
    : sym
    ? `<span><i style="border-top:2px solid var(--good)"></i>цель ±50 bps</span>
       <span><i style="border-top:2px solid var(--critical)"></i>стоп ±20 bps</span>
       <span style="color:var(--ink-muted)">Стороны пока нет — показан масштаб:
         насколько далеко цель и стоп от текущей цены. Направление появится,
         когда детекторы заговорят.</span>`
    : `<span><i style="border-top:2px solid var(--accent)"></i>вход</span>
       <span><i style="border-top:2px solid var(--critical)"></i>стоп</span>
       <span><i style="border-top:2px solid var(--good)"></i>цель</span>
       <span style="color:var(--ink-muted)">${pos
        ? "позиция открыта — линии сплошные"
        : "план: пунктир, пока вход не разрешён"}</span>`;

  const host = $("spark"), tip = $("tip");
  host.onmousemove = (e) => {
    const r = host.getBoundingClientRect();
    const sx = r.width / CH.w, sy = r.height / CH.h;
    const i = clamp(Math.round(((e.clientX - r.left) / sx - CH.l)
      / (CH.w - CH.l - CH.r) * (n - 1)), 0, n - 1);
    const d = mid[i];
    host.querySelector("#cx").setAttribute("x1", px(i));
    host.querySelector("#cx").setAttribute("x2", px(i));
    host.querySelector("#cx").setAttribute("opacity", "1");
    host.querySelector("#cdot").setAttribute("cx", px(i));
    host.querySelector("#cdot").setAttribute("cy", py(d.m));
    host.querySelector("#cdot").setAttribute("opacity", "1");
    const tm = new Date(d.t).toLocaleTimeString("ru-RU");
    tip.innerHTML = `<b>${d.m.toFixed(digits)}</b><br>спред ${fmt(d.s, 2)} bps<br>${tm}`;
    tip.style.opacity = "1";
    tip.style.left = clamp(px(i) * sx + 12, 0, r.width - 140) + "px";
    tip.style.top = clamp(py(d.m) * sy - 10, 0, r.height - 60) + "px";
  };
  host.onmouseleave = () => {
    tip.style.opacity = "0";
    host.querySelector("#cx")?.setAttribute("opacity", "0");
    host.querySelector("#cdot")?.setAttribute("opacity", "0");
  };
}

/* ── путь к сделке ───────────────────────────────────────────── */
function renderGates(br) {
  if (!br) return;
  let blocked = false;
  $("gates").innerHTML = br.gates.map(g => {
    const first = !g.ok && !blocked;
    if (!g.ok) blocked = true;
    const ic = g.ok
      ? `<span class="ic" style="color:var(--good)">✓</span>`
      : `<span class="ic" style="color:var(--warning)">▲</span>`;
    return `<div class="gate${first ? " blocked" : ""}">
      ${ic}
      <div>
        <div class="head"><span class="lab">${g.label}</span>
          <span class="val">${g.value}</span></div>
        <div class="why">${g.why}</div>
      </div>
    </div>`;
  }).join("");
}

/* ── детекторы ───────────────────────────────────────────────── */
function renderDetectors(br) {
  if (!br) return;
  $("dets").innerHTML = br.votes.map(v => {
    const pct = clamp(Math.abs(v.value), 0, 1) * 50;
    const side = v.value >= 0 ? "right:50%" : "left:50%";
    const colour = v.value >= 0 ? "var(--buy)" : "var(--sell)";
    const fill = v.available && Math.abs(v.value) > 0.001
      ? `<div class="fill" style="${v.value >= 0 ? "left:50%" : "right:50%"};
           width:${pct}%;background:${colour}"></div>` : "";
    const num = v.available
      ? `<span class="num">${v.value >= 0 ? "+" : ""}${fmt(v.value, 2)}</span>`
      : `<span class="num">молчит</span>`;
    return `<div class="det${v.available ? "" : " silent"}">
      <div class="row"><span class="name">${v.label}</span>${num}</div>
      <div class="why">${v.available ? v.why : v.reason}</div>
      <div class="bar"><div class="mid"></div>${fill}</div>
    </div>`;
  }).join("");

  const s = br.score, thr = br.threshold;
  const reached = Math.abs(s) >= thr;
  $("score").innerHTML = `
    <div class="det" style="border-top:1px solid var(--grid);padding-top:10px">
      <div class="row">
        <span class="name">Итог</span>
        <span class="num" style="color:${reached ? "var(--good)" : "var(--ink-2)"}">
          ${s >= 0 ? "+" : ""}${fmt(s, 3)} из ±${fmt(thr, 2)}</span>
      </div>
      <div class="why">${reached
        ? "Порог взят: мнения сошлись достаточно сильно."
        : "Порог не взят — робот не входит. Это штатное состояние большую часть времени."}
        Согласны ${br.agree} детекторов из ${br.min_agree} необходимых.</div>
      <div class="bar"><div class="mid"></div>
        <div class="fill" style="${s >= 0 ? "left:50%" : "right:50%"};
          width:${clamp(Math.abs(s), 0, 1) * 50}%;
          background:${s >= 0 ? "var(--buy)" : "var(--sell)"}"></div>
      </div>
    </div>`;
}

/* ── экономика круга ─────────────────────────────────────────── */
function renderEconomics(br) {
  if (!br) return;
  const e = br.economics;
  const parts = [
    ["Комиссии", e.fee_bps, "var(--sell)"],
    ["Спред", e.spread_bps, "var(--serious)"],
    ["Проскальзывание", e.slippage_bps, "var(--warning)"],
    ["Фандинг", e.funding_bps, "var(--ink-muted)"],
  ].filter(p => p[1] > 0.001);
  const total = e.total_bps || 1;

  const bar = parts.map(([n, v, c]) =>
    `<span style="width:${(v / total * 100).toFixed(1)}%;background:${c}"
       title="${n}: ${fmt(v, 2)} bps">${v / total > 0.14 ? fmt(v, 1) : ""}</span>`
  ).join("");

  const legend = parts.map(([n, v, c]) =>
    `<span><i style="background:${c}"></i>${n} ${fmt(v, 2)}</span>`).join("");

  const ratio = e.target_bps / e.total_bps;
  const verdict = ratio >= 3
    ? ["Цель втрое перекрывает издержки — сделка имеет смысл.", "var(--good)"]
    : ["Цель не перекрывает издержки с запасом — такую сделку брать нельзя.", "var(--critical)"];

  $("eco").innerHTML = `
    <div>
      <div style="display:flex;justify-content:space-between;font-size:12px;
                  color:var(--ink-2);margin-bottom:5px">
        <span>Круг обходится в</span><b style="color:var(--ink)">${fmt(e.total_bps, 2)} bps</b>
      </div>
      <div class="eco-bar">${bar}</div>
      <div class="eco-legend" style="margin-top:7px">${legend}</div>
    </div>
    <dl class="kv">
      <dt>Цель сделки</dt><dd>${fmt(e.target_bps, 0)} bps</dd>
      <dt>Стоп</dt><dd>${fmt(e.stop_bps, 0)} bps</dd>
      <dt class="pro">Требуется гейтом размера</dt><dd class="pro">${fmt(e.required_bps, 1)} bps</dd>
      <dt>Доля успеха для безубытка</dt><dd>${fmt(e.breakeven_win_rate * 100, 1)} %</dd>
    </dl>
    <div style="font-size:12.5px;color:${verdict[1]}">${verdict[0]}</div>
    <div class="hint" style="margin:0">Издержки платятся дважды — на входе и на выходе.
      Поэтому цель обязана быть кратно больше, а не просто больше.</div>`;
}

/* ── стакан ──────────────────────────────────────────────────── */
function renderBook(s) {
  const d = s.depth || { bids: [], asks: [] };
  if (!d.bids.length || !d.asks.length) { return; }
  const all = [...d.bids, ...d.asks].map(x => x[1]);
  const max = Math.max(...all) || 1;
  const row = (p, q, cls) =>
    `<div class="lvl ${cls}"><div class="wash" style="width:${(q / max * 100).toFixed(1)}%"></div>
      <span class="px">${p}</span><span class="qty">${Math.round(q).toLocaleString("ru-RU")}</span></div>`;

  const asks = d.asks.slice(0, 8).reverse().map(([p, q]) => row(p, q, "ask")).join("");
  const bids = d.bids.slice(0, 8).map(([p, q]) => row(p, q, "bid")).join("");

  const sb = d.bids.slice(0, 10).reduce((a, x) => a + x[1], 0);
  const sa = d.asks.slice(0, 10).reduce((a, x) => a + x[1], 0);
  const imb = (sb - sa) / (sb + sa || 1);
  const word = Math.abs(imb) < 0.1 ? "равновесие"
    : imb > 0 ? "перевес покупателей" : "перевес продавцов";

  $("book").innerHTML = asks + `
    <div class="mid-row">
      <span style="color:var(--ink-2);font-size:12px">перевес</span>
      <b style="color:${imb >= 0 ? "var(--buy)" : "var(--sell)"}">${word}</b>
      <span class="pro num">${imb >= 0 ? "+" : ""}${fmt(imb, 2)}</span>
    </div>` + bids;
}

/* ── лента ───────────────────────────────────────────────────── */
function renderTape(s) {
  const f = s.flow || 0;
  const word = Math.abs(f) < 0.1 ? "поровну"
    : f > 0 ? "покупатели активнее" : "продавцы активнее";
  $("flow").innerHTML = `
    <div class="det" style="border:0;padding:0">
      <div class="row"><span class="name">За последнюю минуту</span>
        <span class="num">${word}<span class="pro"> · ${f >= 0 ? "+" : ""}${fmt(f, 2)}</span></span></div>
      <div class="bar"><div class="mid"></div>
        <div class="fill" style="${f >= 0 ? "left:50%" : "right:50%"};
          width:${clamp(Math.abs(f), 0, 1) * 50}%;
          background:${f >= 0 ? "var(--buy)" : "var(--sell)"}"></div></div>
    </div>`;

  const tape = s.tape || [];
  if (!tape.length) return;
  $("tape").innerHTML = tape.map(t => {
    const cls = t.side === "Buy" ? "buy" : "sell";
    const tm = new Date(t.t).toLocaleTimeString("ru-RU");
    return `<div class="t ${cls}"><span class="tm">${tm}</span>
      <span class="p">${t.p}</span><span class="v">${t.v}</span></div>`;
  }).join("");
}

/* ── виртуальная торговля ────────────────────────────────────────
 *
 * Разделена на две карточки, потому что отвечает на два разных
 * вопроса. «Виртуальная торговля» — что происходит сейчас и сколько
 * уже набрано. «Проверка предположений» — где расчёт расходится с
 * живым потоком; это и есть продукт всего режима, и в общей куче
 * счётчиков он терялся.
 */
function renderPaper(s) {
  const p = s.paper || {};
  if (!p.alive) {
    $("paper").innerHTML = `<div class="empty">Не запущена. Запустить:
      <code>.venv\\Scripts\\python.exe ops\\paper.py</code></div>`;
    $("checks").innerHTML = `<div class="empty">Проверять нечего: допущения
      сверяются на живом потоке, а виртуальная торговля не запущена.</div>`;
    return;
  }
  const q = p.quality || {}, c = q.counters || {};
  const n = c.closed || 0;

  const now = p.position
    ? `<span style="color:${p.position.side === "Buy" ? "var(--buy)" : "var(--sell)"}">
         ${p.position.side === "Buy" ? "в покупке" : "в продаже"}</span>
       от ${p.position.entry}, держит ${Math.round(p.position.held_sec)} с`
    : p.pending
    ? `заявка ${p.pending.side === "Buy" ? "на покупку" : "на продажу"}
       по ${p.pending.price}, ждёт ${Math.round(p.pending.waiting_sec)} с`
    : "вне рынка";

  const avg = c.net_bps_avg;
  const colour = avg == null ? "var(--ink-2)"
    : avg > 0 ? "var(--good)" : "var(--critical)";

  const stat = `
    <div class="pstat">
      <div><b>${n}</b><span>сделок закрыто</span></div>
      <div><b style="color:${colour}">${avg == null ? "—" : (avg >= 0 ? "+" : "") + fmt(avg, 2)}</b>
           <span>средняя, bps</span></div>
      <div><b>${c.fill_rate == null ? "—" : fmt(c.fill_rate * 100, 0) + "%"}</b>
           <span>заявок исполнилось</span></div>
      <div><b>${n ? fmt(c.wins / n * 100, 0) + "%" : "—"}</b>
           <span>прибыльных</span></div>
      <div><b style="font-size:15px;font-weight:500">${now}</b>
           <span>сейчас</span></div>
    </div>`;

  const reasons = c.reasons || {};
  const rl = Object.entries(reasons).map(([k, v]) =>
    `<span class="chip">${esc(REASON_RU[k] || k)} <b>${v}</b></span>`).join("");

  $("paper").innerHTML = stat
    + (rl ? `<div class="chips" style="margin-top:4px">${rl}</div>` : "")
    + `<p class="hint" style="margin:12px 0 0">Работает ${
        fmt((p.uptime_sec || 0) / 3600, 1)} ч${
        p.testnet ? " · тестовая сеть" : ""}. Денег в этих сделках нет:
      ошибаться здесь можно и нужно.</p>`;

  $("checks").innerHTML = (q.checks || []).map((ch) => `
    <div class="check">
      <div><div class="lab">${esc(ch.label)}</div></div>
      <div class="v"><span class="cap">робот считает</span>${esc(ch.assumed)}</div>
      <div class="v real"><span class="cap">на самом деле</span>${esc(ch.actual)}</div>
      <div class="note">${esc(ch.note)}</div>
    </div>`).join("")
    + `<p class="hint" style="margin:12px 0 0">Доля исполнения — ВЕРХНЯЯ граница:
      очередь не моделируется, и реальная заявка исполняется реже.</p>`;
}

/* ── новостной фон ───────────────────────────────────────────────── */
const KIND = { exchange: ["биржа", "var(--critical)"],
               instrument: ["инструмент", "var(--accent)"],
               macro: ["рынок", "var(--ink-2)"] };
const SEV = { critical: "‼", important: "!", background: "·" };

function renderNews(s) {
  const n = s.news;
  if (!n) {
    $("news").innerHTML = `<div class="empty">Наблюдатель не запущен. Запустить:
      <code>.venv\\Scripts\\python.exe ops\\news_watch.py</code></div>`;
    return;
  }
  const v = n.veto || {};
  const head = v.active
    ? `<div class="gate blocked"><span class="ic" style="color:var(--warning)">▲</span>
        <div><div class="head"><span class="lab">Вход приостановлен новостью</span>
          <span class="val">ещё ${Math.round((v.left_sec || 0) / 60)} мин</span></div>
        <div class="why">${(v.item || {}).title || ""}</div></div></div>`
    : `<div class="gate"><span class="ic" style="color:var(--good)">✓</span>
        <div><div class="head"><span class="lab">Фон спокойный</span>
          <span class="val">${n.total} событий в истории</span></div>
        <div class="why">Критических событий за последние ${n.quiet_min} мин нет.
          Торговлю ничего не блокирует.</div></div></div>`;

  const rows = (n.recent || []).slice(0, 10).map(it => {
    const [kl, kc] = KIND[it.kind] || [it.kind, "var(--ink-2)"];
    const tm = new Date(it.ts_ms).toLocaleString("ru-RU",
      { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    const title = it.url
      ? `<a href="${it.url}" target="_blank" rel="noopener noreferrer">${it.title}</a>`
      : it.title;
    return `<div class="news-i">
      <span style="color:${it.severity === "critical" ? "var(--critical)"
        : it.severity === "important" ? "var(--warning)" : "var(--ink-muted)"}"
        >${SEV[it.severity] || "·"}</span>
      <div>${title}<div class="meta pro">${it.matched.join(", ")}</div></div>
      <span class="meta"><span style="color:${kc}">${kl}</span> · ${it.source} · ${tm}</span>
    </div>`;
  }).join("");

  $("news").innerHTML = head +
    `<div style="margin-top:10px">${rows || '<div class="empty">Событий нет</div>'}</div>`;
}

/* ── события ─────────────────────────────────────────────────── */
// Уровни алертов приходят кодом (INFO/REPORT/WARN/CRITICAL). На экране
// у них должно быть человеческое имя и статусный цвет — код уровня
// сам по себе ничего не сообщает тому, кто его не писал.
const LEVELS = {
  INFO: ["сообщение", "var(--ink-muted)"],
  REPORT: ["отчёт", "var(--ink-2)"],
  WARN: ["предупреждение", "var(--warning)"],
  WARNING: ["предупреждение", "var(--warning)"],
  ERROR: ["ошибка", "var(--serious)"],
  CRITICAL: ["критично", "var(--critical)"],
};

function renderAlerts(s) {
  const a = s.alerts || {};
  const rows = (a.recent || []).map(r =>
    `<div class="gate"><span class="ic">•</span>
      <div><div class="head"><span class="lab">${r.title || "—"}</span>
        <span class="val" style="color:${(LEVELS[r.level] || [])[1] || "var(--ink-2)"}">
          ${(LEVELS[r.level] || [r.level || ""])[0]}</span></div>
      <div class="why">${new Date(r.ts).toLocaleString("ru-RU")}</div></div></div>`).join("");
  $("alerts").innerHTML = rows ||
    `<div class="empty">Событий нет. Очередь: ${a.pending || 0} ждёт,
      ${a.sent || 0} отправлено, ${a.failed || 0} не ушло.</div>`;
}

/* ── подключение биржи ───────────────────────────────────────
 *
 * Единственное место панели, которое что-то отправляет. Три правила,
 * без которых эта карточка вредит больше, чем помогает.
 *
 * Перерисовка не трогает открытую форму. Состояние обновляется раз в
 * секунду; если рисовать карточку каждый раз, введённый ключ будет
 * стираться под пальцами — набрать секрет из 36 символов станет
 * невозможно.
 *
 * Секрет не хранится нигде: он уходит из поля в запрос и стирается
 * сразу после. Ни localStorage, ни autocomplete, ни повтор при ошибке.
 *
 * Отказ показывается целиком, как его объяснил сервер. «Не удалось
 * подключиться» — бесполезное сообщение: у отказа всегда есть
 * конкретная причина, и пользователю нужна именно она.
 */
let connSig = null;        // что нарисовано сейчас
let connEditing = false;   // форма открыта — перерисовывать нельзя
let connBusy = false;      // запрос в полёте

function connMsg(html) { const n = $("conn-msg"); if (n) n.innerHTML = html; }

function connProblems(c) {
  if (!c) return "";
  return (c.problems || []).map(p => `<div class="conn-bad">✗ ${esc(p)}</div>`).join("")
    + (c.warnings || []).map(w => `<div class="conn-warn">⚠ ${esc(w)}</div>`).join("");
}

function connForm(net) {
  const main = net === "MAINNET";
  return `
  <ol class="conn-steps">
    <li>Откройте <b>${main ? "bybit.com" : "testnet.bybit.com"}</b> →
      API → Create New Key → System-generated</li>
    <li>Права: <b>Read-Write</b>, Unified Trading → <b>Trade</b></li>
    <li><b>Withdraw не отмечать</b> — робот такой ключ не примет</li>
    <li>IP: адрес этой машины. Без привязки утёкший ключ работает
      откуда угодно</li>
  </ol>
  <div class="conn-form">
    <label>API Key
      <input type="text" id="conn-key" autocomplete="off" spellcheck="false"
             placeholder="например, AbCdEf01…"></label>
    <label>API Secret
      <input type="password" id="conn-secret" autocomplete="off"
             spellcheck="false" placeholder="показывается биржей один раз"></label>
    <label class="conn-row" style="gap:7px">
      <input type="checkbox" id="conn-main" ${main ? "checked" : ""}>
      боевой счёт (настоящие деньги)</label>
    <div id="conn-confirm-box" style="display:${main ? "block" : "none"}">
      <div class="conn-warn">Ключ даст роботу распоряжаться настоящими
        деньгами. Наберите слово БОЕВОЙ, чтобы подтвердить.</div>
      <input type="text" id="conn-confirm" autocomplete="off"
             placeholder="БОЕВОЙ">
    </div>
    <div class="conn-row">
      <button class="act" id="conn-go">Подключить</button>
      <span class="conn-note">Ключ проверяется на бирже до сохранения.</span>
    </div>
    <div id="conn-msg"></div>
  </div>`;
}

function renderConn(s) {
  const e = s.exchange || {};
  const c = e.last_check || null;
  const sig = JSON.stringify([e.connected, e.key, e.network, e.verified,
                              e.conflict, c && c.checked_at_ms]);
  if (connEditing || connBusy || sig === connSig) return;
  connSig = sig;

  if (!e.connected) {
    $("conn").innerHTML = connForm("TESTNET");
    wireConn();
    return;
  }

  const when = c && c.checked_at_ms
    ? new Date(c.checked_at_ms).toLocaleString("ru-RU") : null;
  const head = e.verified
    ? `<span class="conn-ok">✓ подключён</span>`
    : `<span class="conn-warn">ключ есть, но биржа его не подтвердила</span>`;
  const money = c && c.equity !== null && c.equity !== undefined
    ? ` · баланс <b>${esc(c.equity)}</b> USDT` : "";
  const ips = c && c.ips && c.ips.length && c.ips[0] !== "*"
    ? ` · IP ${esc(c.ips.join(", "))}` : "";

  $("conn").innerHTML = `
    <div class="conn-line">${head}
      <span class="key">${esc(e.key)}</span>
      <span class="conn-note">${esc(e.network)}${money}${ips}</span>
    </div>
    ${when ? `<div class="conn-note">проверен ${esc(when)}</div>` : ""}
    ${e.conflict ? `<div class="conn-warn">⚠ в файле лежит другой ключ
      (${esc(e.file_key)}): этот процесс получил ключ из окружения и о
      подмене не знает. Перезапустите робота, чтобы все процессы работали
      с одним ключом.</div>` : ""}
    ${connProblems(c)}
    <div class="conn-row" style="margin-top:10px">
      <button class="ghost" id="conn-check">Проверить на бирже</button>
      <button class="ghost" id="conn-edit">Другой ключ</button>
      <button class="ghost" id="conn-forget">Отключить</button>
    </div>
    <div id="conn-msg"></div>`;

  $("conn-check").onclick = () => connPost("/api/exchange/check", {});
  $("conn-edit").onclick = () => {
    connEditing = true;
    $("conn").innerHTML = connForm(e.network);
    wireConn();
  };
  $("conn-forget").onclick = () => {
    if (!window.confirm("Робот забудет ключ и перестанет торговать. "
      + "На самой бирже ключ останется — удалите его в кабинете Bybit, "
      + "если отключаете робота насовсем.")) return;
    connPost("/api/exchange/forget", {});
  };
}

function wireConn() {
  const main = $("conn-main");
  if (main) main.onchange = () => {
    $("conn-confirm-box").style.display = main.checked ? "block" : "none";
  };
  ["conn-key", "conn-secret", "conn-confirm"].forEach(id => {
    const n = $(id);
    if (n) n.onfocus = () => { connEditing = true; };
  });
  const go = $("conn-go");
  if (go) go.onclick = () => {
    const key = ($("conn-key").value || "").trim();
    const secret = ($("conn-secret").value || "").trim();
    if (!key || !secret) { connMsg(`<div class="conn-bad">Нужны и ключ, и секрет.</div>`); return; }
    const testnet = !$("conn-main").checked;
    const confirm = $("conn-confirm") ? ($("conn-confirm").value || "").trim() : "";
    $("conn-secret").value = "";        // секрет в поле не задерживается
    connPost("/api/exchange/connect", { key, secret, testnet, confirm });
  };
}

async function connPost(url, body) {
  if (connBusy) return;
  connBusy = true;
  const go = $("conn-go");
  if (go) { go.disabled = true; go.textContent = "Спрашиваю биржу…"; }
  connMsg(`<div class="conn-note">Спрашиваю биржу…</div>`);
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (r.status === 501 || r.status === 405 || r.status === 404) {
      // Разметку и скрипт панель перечитывает с диска на каждый запрос,
      // а свой Python — только при старте. После обновления кода карточка
      // появляется сразу, а обработчик POST — нет, и отказ выглядит как
      // поломка ввода. Это не она.
      connMsg(`<div class="conn-bad">✗ панель работает на старом коде
        (${r.status}): разметка обновилась, а процесс — нет.</div>
        <div class="conn-note">Перезапустите робота — и карточка заработает.
        Пока он не перезапущен, ключ можно ввести в консоли:
        <b>python ops/bybit_login.py</b></div>`);
      return;
    }
    if (!r.ok || d.ok === false) {
      connMsg(connProblems(d.check)
        || `<div class="conn-bad">✗ ${esc(d.error || ("отказ " + r.status))}</div>`);
      return;
    }
    connEditing = false;
    connSig = null;                      // следующий опрос перерисует карточку
    const c = d.check || {};
    connMsg(`<div class="conn-ok">✓ ${esc(c.summary || "готово")}</div>`
      + (c.warnings || []).map(w => `<div class="conn-warn">⚠ ${esc(w)}</div>`).join("")
      + `<div class="conn-note">Робот подхватит ключ сам: супервизор
         перезапустит торговый процесс в течение нескольких секунд.</div>`);
  } catch (err) {
    connMsg(`<div class="conn-bad">✗ панель не ответила: ${esc(err.message)}</div>`);
  } finally {
    connBusy = false;
    if (go) { go.disabled = false; go.textContent = "Подключить"; }
  }
}

/* ── деньги: кошелёк и настоящие сделки ────────────────────────
 *
 * Словари переводов держатся здесь, а не в разметке: коды приходят от
 * робота (`Veto`, `CloseReason`, `Mode` из src/cashmash/core/types.py),
 * и человеку их показывать нельзя — `not_connected` не объясняет
 * ничего. Код, которого в словаре нет, печатается как есть: пропажа
 * перевода не должна превращаться в пустое место.
 */
const MODE_RU = {
  LIVE: "торгует",
  SIGNAL_ONLY: "считает сигналы, но не входит",
  MANAGE_ONLY: "сопровождает открытое, новых сделок не открывает",
  FLATTEN: "закрывает позиции",
  PAUSED: "остановлен",
};
const VETO_RU = {
  none: "без запрета",
  stale_data: "данные не свежие",
  book_desync: "стакан рассинхронизирован",
  clock_drift: "часы разошлись с биржей",
  warmup: "индикаторы не прогреты",
  rate_budget: "исчерпан лимит запросов к бирже",
  ban_cooldown: "биржа временно отказывает",
  not_connected: "счёт не подключён",
  reconcile_failed: "сверка с биржей не сошлась",
  instrument_status: "инструмент недоступен",
  spread: "спред слишком широк",
  regime: "режим рынка неподходящий",
  funding_window: "окно фандинга",
  funding_extreme: "фандинг слишком дорог",
  session: "время вне сессии",
  news: "новостной фон",
  cost: "сделка не окупает издержки",
  limit_daily_loss: "дневной лимит убытка",
  limit_weekly_loss: "недельный лимит убытка",
  limit_drawdown: "просадка",
  limit_trades: "дневной лимит числа сделок",
  loss_streak: "серия убытков",
  margin: "маржи не хватает",
  liquidation_distance: "ликвидация слишком близко",
  min_notional: "размер меньше минимального",
  max_leverage: "плечо выше допустимого",
  position_open: "позиция уже открыта",
  cooldown: "пауза после сделки",
  post_only_expired: "пассивная заявка не дождалась цены",
};
const REASON_RU = {
  tp: "цель", sl: "стоп", trail: "трейлинг",
  time_soft: "мягкий тайм-стоп", time_hard: "жёсткий тайм-стоп",
  funding: "фандинг", signal: "сигнал развернулся",
  emergency: "аварийное закрытие", manual: "вручную",
};
const STAGE_RU = {
  OPENED: "открыта", BREAKEVEN: "стоп в безубытке",
  PARTIAL_TAKEN: "часть зафиксирована", TRAILING: "трейлинг",
  CLOSING: "закрывается",
};

const isBuy = (v) => {
  const s = String(v || "").toUpperCase();
  return s.startsWith("B") || s === "LONG";
};
const sideRu = (v) => (isBuy(v) ? "покупка" : "продажа");
const sideCls = (v) => (isBuy(v) ? "buy" : "sell");
const signed = (x, n = 2) =>
  x === null || x === undefined || Number.isNaN(Number(x))
    ? "—" : (Number(x) >= 0 ? "+" : "") + Number(x).toFixed(n);
const tone = (x) => (x === null || x === undefined ? "" : Number(x) >= 0 ? "up" : "down");
/* Цена приходит из Decimal и бывает длиной в двадцать знаков: лишние
   разряды не значат ничего, кроме того, что делилось нацело не всё. */
const px = (v) => (v === null || v === undefined || v === ""
  ? "—" : Number(v).toFixed(5).replace(/0+$/, "").replace(/\.$/, ""));
const hhmm = (ms) => new Date(ms).toLocaleTimeString("ru-RU",
  { hour: "2-digit", minute: "2-digit" });

function ago(ms, now) {
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 60) return s + " с назад";
  if (s < 3600) return Math.round(s / 60) + " мин назад";
  if (s < 86400) return (s / 3600).toFixed(1).replace(".0", "") + " ч назад";
  return Math.round(s / 86400) + " сут назад";
}
function dur(sec) {
  if (sec === null || sec === undefined) return "—";
  if (sec < 60) return Math.round(sec) + " с";
  if (sec < 3600) return Math.round(sec / 60) + " мин";
  return (sec / 3600).toFixed(1) + " ч";
}

/* ── кошелёк ─────────────────────────────────────────────────────
 *
 * Главное число панели. Рядом с ним обязаны стоять две вещи: какой
 * это счёт и откуда число взялось. Баланс без происхождения — самая
 * опасная строка здесь: снятый три часа назад, он выглядит как
 * «сейчас», и решение принимается по несуществующим деньгам.
 */
/* Разметку и скрипт панель перечитывает с диска на каждый запрос, а
   свой Python — только при старте. После обновления кода карточка
   появляется сразу, а поле в ответе — нет. Промолчать об этом здесь
   нельзя: пустое поле выглядит как «счёт не подключён», то есть как
   утверждение о деньгах, которого никто не проверял. */
const STALE = `<div class="when">панель работает на старом коде: разметка
  обновилась, а процесс — нет. Перезапустите робота.</div>`;

function renderWallet(s) {
  if (!s.wallet) {
    $("wallet").innerHTML = `<div class="top">
      <span class="cap">Средства кошелька</span>
      <span class="big">—</span><span class="unit">USDT</span></div>` + STALE;
    return;
  }
  const w = s.wallet;
  const known = w.equity !== null && w.equity !== undefined;

  const badges = [];
  if (w.connected) {
    badges.push(`<span class="badge ${w.testnet ? "" : "real"}">${
      w.testnet ? "тестовый счёт" : "боевой счёт"}</span>`);
  }
  if (w.live) badges.push(`<span class="badge live">живой</span>`);
  else if (w.source === "check") badges.push(`<span class="badge">с проверки ключа</span>`);
  if (w.key) badges.push(`<span class="badge pro">${esc(w.key)}</span>`);

  const d = w.day;
  const day = d && d.realized_pnl !== null && d.realized_pnl !== undefined
    ? `<div class="day">
         <b class="${tone(d.realized_pnl)}">${signed(d.realized_pnl)}</b>
         <span>за сегодня · сделок ${d.trades}${
           d.pct === null || d.pct === undefined ? "" : " · " + signed(d.pct, 2) + "%"}</span>
       </div>`
    : "";

  let when = "";
  if (w.ts_ms && w.live) {
    when = `по данным торгового процесса, ${ago(w.ts_ms, s.now_ms)}`;
  } else if (w.ts_ms) {
    when = `на момент проверки ключа: ${new Date(w.ts_ms).toLocaleString("ru-RU")}`
      + ` · ${ago(w.ts_ms, s.now_ms)}`;
  } else if (!w.connected) {
    when = "счёт не подключён — подключить можно во вкладке «Торговля»";
  }

  const notes = (w.notes || []).map(
    (n) => `<div class="wnote"><i>⚠</i><span>${esc(n)}</span></div>`).join("");

  $("wallet").innerHTML = `
    <div class="top">
      <span class="cap">Средства кошелька</span>
      <span class="big">${known ? esc(w.equity) : "—"}</span>
      <span class="unit">USDT</span>
      ${badges.join("")}
      <span class="spacer"></span>
      ${day}
    </div>
    ${when ? `<div class="when">${esc(when)}</div>` : ""}
    ${notes}`;
}

/* ── настоящие сделки ────────────────────────────────────────────
 *
 * Отдельная карточка, а не строка в общей ленте: настоящая сделка —
 * единственное событие панели, которое стоит денег. Пустой список
 * здесь нормален, и объяснить пустоту обязательно: «сделок нет» и
 * «панель сломалась» иначе выглядят одинаково.
 */
function tradeRow(t) {
  const pnl = t.net_pnl === null || t.net_pnl === undefined ? null : Number(t.net_pnl);
  return `<div class="tr">
    <span class="tm">${hhmm(t.closed_ms || t.opened_ms)}</span>
    <span class="sd ${sideCls(t.side)}">${sideRu(t.side)}</span>
    <span class="px">${px(t.entry)} → ${px(t.close_price)}</span>
    <span class="usd ${tone(pnl)}">${pnl === null ? "—" : signed(pnl)}</span>
    <span class="bps ${tone(t.net_bps)}">${t.net_bps === null || t.net_bps === undefined
      ? "—" : signed(t.net_bps, 1) + " bps"}</span>
    <span class="rs">${esc(REASON_RU[t.reason] || t.reason || "—")} · ${dur(t.held_sec)}</span>
  </div>`;
}

function renderTrades(s) {
  if (!s.trades) {
    $("trades").innerHTML = `<div class="empty">Сделки не прочитаны.</div>` + STALE;
    return;
  }
  const t = s.trades;
  const hb = (s.bot || {}).heartbeat || {};
  if (!t.available) {
    $("trades").innerHTML = `<div class="empty">${esc(t.note || "Данных нет")}.
      Сделки появятся здесь, как только торговый процесс их совершит.</div>`;
    return;
  }

  const tot = t.totals || {};
  const n = tot.closed || 0;
  const open = t.open || [];
  const net = tot.net_pnl === null || tot.net_pnl === undefined ? null : Number(tot.net_pnl);

  const stat = `<div class="pstat">
    <div><b>${n}</b><span>сделок закрыто</span></div>
    <div><b class="${tone(net)}">${net === null ? "—" : signed(net)}</b>
         <span>итог, USDT</span></div>
    <div><b>${tot.win_rate === null || tot.win_rate === undefined
      ? "—" : fmt(tot.win_rate * 100, 0) + "%"}</b><span>прибыльных</span></div>
    <div><b class="${tone(tot.avg_bps)}">${tot.avg_bps === null || tot.avg_bps === undefined
      ? "—" : signed(tot.avg_bps, 1)}</b><span>средняя, bps</span></div>
    <div class="pro"><b>${tot.fee === null || tot.fee === undefined
      ? "—" : fmt(Number(tot.fee), 2)}</b><span>комиссии, USDT</span></div>
  </div>`;

  const openHtml = open.map((p) => `
    <div class="posbox ${sideCls(p.side)}">
      <div class="row">
        <b>${sideRu(p.side)} ${esc(p.qty || "")} от ${px(p.entry)}</b>
        <span class="conn-note">открыта ${dur((s.now_ms - p.opened_ms) / 1000)} назад${
          p.degraded ? " · сопровождение ограничено" : ""}</span>
      </div>
      <dl class="kv">
        <dt>стоп</dt><dd>${px(p.sl)}</dd>
        <dt>цель</dt><dd>${px(p.tp)}</dd>
        <dt>стадия</dt><dd>${esc(STAGE_RU[p.stage] || p.stage || "—")}</dd>
        <dt class="pro">риск, USDT</dt><dd class="pro">${esc(p.r_usdt || "—")}</dd>
      </dl>
    </div>`).join("");

  let list = "";
  if (n) {
    list = `<div class="trades scroll" style="margin-top:12px">${
      (t.closed || []).map((x) => tradeRow(x)).join("")}</div>`;
    if (n > (t.closed || []).length) {
      list += `<p class="hint" style="margin:8px 0 0">Показаны последние ${
        (t.closed || []).length} из ${n}; итоги сверху — по всем.</p>`;
    }
  }

  // Пустота объясняется, а не подразумевается. Робот принимает решение
  // несколько раз в секунду, и его отказы — самый прямой ответ на
  // вопрос «почему сделок нет»; они уже посчитаны торговым процессом.
  let why = "";
  if (!n && !open.length) {
    const d = t.last_decision;
    const vetoes = Object.entries(hb.vetoes || {}).sort((a, b) => b[1] - a[1]).slice(0, 4);
    const total = Object.values(hb.vetoes || {}).reduce((a, b) => a + b, 0);
    why = `<div class="empty">Настоящих сделок ещё не было.</div>`
      + (hb.mode ? `<p class="hint" style="margin:4px 0 0">Торговый процесс в режиме
          <b>${esc(hb.mode)}</b> — ${esc(MODE_RU[hb.mode] || "режим нестандартный")}.</p>` : "")
      + (d ? `<p class="hint" style="margin:6px 0 0">Последнее решение — не входить:
          <b>${esc(VETO_RU[d.veto] || d.veto)}</b>${
            d.reason ? " (" + esc(d.reason) + ")" : ""}, ${ago(d.ts_ms, s.now_ms)}.</p>` : "")
      + (vetoes.length ? `<div class="chips" style="margin-top:10px">${
          vetoes.map(([k, v]) => `<span class="chip">${esc(VETO_RU[k] || k)} <b>${v}</b></span>`)
            .join("")}</div>
          <p class="hint" style="margin:8px 0 0">Всего отказов за этот запуск:
            ${total}. Что именно не сошлось — в карточке «Путь к сделке» ниже.</p>` : "");
  }

  // Заявки в полёте — это техника исполнения, а не результат, и
  // новичку они говорят меньше, чем пугают: скрыты вместе с остальным
  // «профи»-слоем. Показываются только незавершённые: история заявок
  // уже рассказана списком сделок.
  const live = (t.orders || []).filter(
    (o) => ["RESERVED", "SENT", "UNKNOWN"].includes(String(o.state || "").toUpperCase()));
  const orders = live.length ? `
    <h2 style="margin-top:16px">Заявки в полёте</h2>
    <p class="hint">Отправлены, исход ещё не известен. При перезапуске их
      подбирает сверка с биржей.</p>
    ${live.map((o) => `<div class="ord">
      <span class="tm">${hhmm(o.ts_ms)}</span>
      <span>${esc(o.action || "")} ${o.side ? sideRu(o.side) : ""} ${esc(o.qty || "")}</span>
      <span>${px(o.price)}</span>
      <span class="st">${esc(o.state)}${o.attempts ? " · попыток " + o.attempts : ""}</span>
      ${o.last_error ? `<span class="err">${esc(o.last_error)}</span>` : ""}
    </div>`).join("")}` : "";

  $("trades").innerHTML = (why || stat) + openHtml + list
    + `<div class="pro">${orders}</div>`;
}

/* ── журнал виртуальных сделок ───────────────────────────────── */
function renderPaperTrades(s) {
  const rows = (s.paper || {}).trades || [];
  if (!rows.length) {
    $("ptrades").innerHTML = `<div class="empty">Закрытых виртуальных сделок
      пока нет. Первая появится через несколько минут после запуска —
      сначала должны прогреться индикаторы.</div>`;
    return;
  }
  $("ptrades").innerHTML = `<div class="trades paper scroll">${rows.map((r) => `
    <div class="tr">
      <span class="tm">${hhmm(r.ts_ms)}</span>
      <span class="sd ${sideCls(r.side)}">${sideRu(r.side)}</span>
      <span class="px">${px(r.entry)} → ${px(r.exit)}</span>
      <span class="bps ${tone(r.net_bps)}">${signed(r.net_bps, 1)} bps</span>
      <span class="rs">${esc(REASON_RU[r.reason] || r.reason)} · ${dur(r.held_sec)}${
        r.regime ? " · " + esc(r.regime) : ""}</span>
    </div>`).join("")}</div>`;
}

/* ── что если ────────────────────────────────────────────────────
 *
 * Единственная карточка панели, где числа задаёт человек. Считает их
 * не браузер: форма уходит в `/api/whatif`, и там работает тот самый
 * `cost_gate`, что стоит в бою и в бэктесте. Копия формулы на
 * JavaScript разошлась бы с оригиналом на первой правке ставок — и
 * разошлась бы молча, показывая «окупается» там, где робот отказывает.
 *
 * Карточка строится ОДИН раз. Панель перерисовывается раз в секунду, и
 * перестроение стирало бы цифру под пальцами — та же причина, по
 * которой не трогают открытую форму подключения биржи.
 */
let wiBuilt = false;
let wiTimer = 0;
let wiReal = null;      /* что измерено на бумаге: подставляется кнопкой */

const wiNum = (id) => ($(id).value || "").trim().replace(",", ".");

function wiQuery() {
  const q = new URLSearchParams();
  const put = (k, v) => { if (v !== "" && v !== null) q.set(k, v); };
  const pct = (v) => (v === "" ? "" : String(Number(v) / 100));
  put("tp", wiNum("wi-tp"));
  put("sl", wiNum("wi-sl"));
  put("p", pct(wiNum("wi-p")));
  put("spread", wiNum("wi-spread"));
  put("timestop", pct(wiNum("wi-ts")));
  put("notional", wiNum("wi-notional"));
  put("maker", $("wi-maker").checked ? "1" : "0");
  return q.toString();
}

function wiFill(inp) {
  $("wi-tp").value = fmt(inp.tp, 0);
  $("wi-sl").value = fmt(inp.sl, 0);
  $("wi-p").value = fmt(inp.p * 100, 1);
  $("wi-spread").value = fmt(inp.spread, 2);
  $("wi-ts").value = fmt(inp.timestop * 100, 0);
  $("wi-notional").value = fmt(inp.notional, 0);
  $("wi-maker").checked = inp.maker;
}

function wiShow(d) {
  const g = d.gate, c = d.cost, t = d.with_timestop, b = d.breakeven;
  const ok = g.passed;
  const real = wiReal && wiReal.win_rate !== null && wiReal.win_rate !== undefined
    ? ` · на бумаге измерено <b>${fmt(wiReal.win_rate * 100, 1)}%</b>` : "";

  $("wi-out").innerHTML = `
    <div class="verdict" style="margin-bottom:10px">
      <div class="dot" style="background:${ok ? "var(--good)" : "var(--warning)"}"></div>
      <div class="verdict-text">
        <strong>${ok ? "Робот взял бы такую сделку" : "Робот отказал бы"}</strong>
        <span>${esc(g.detail)}</span>
      </div>
    </div>
    <dl class="kv">
      <dt>круг обходится в</dt>
      <dd>${fmt(c.total_bps, 2)} bps</dd>
      <dt class="pro">из них комиссия · спред · слиппедж · фандинг</dt>
      <dd class="pro">${fmt(c.fee_bps, 1)} · ${fmt(c.spread_bps, 2)}
        · ${fmt(c.slippage_bps, 1)} · ${fmt(c.funding_bps, 1)}</dd>
      <dt>порог размера (${fmt(d.input.k, 0)} × круг)</dt>
      <dd>${fmt(g.required_bps, 1)} bps ${g.size_ok ? "✓" : "✗"}</dd>
      <dt>эдж до издержек</dt>
      <dd class="${tone(g.gross_bps)}">${signed(g.gross_bps, 1)} bps</dd>
      <dt>эдж после издержек (минимум ${fmt(d.input.edge, 1)})</dt>
      <dd class="${tone(g.net_bps)}">${signed(g.net_bps, 1)} bps ${g.edge_ok ? "✓" : "✗"}</dd>
      <dt>безубыток требует</dt>
      <dd>${fmt(b.win_rate * 100, 1)} %</dd>
      ${d.input.timestop > 0 ? `
        <dt>то же с тайм-стопами</dt>
        <dd>${fmt(b.win_rate_with_timestop * 100, 1)} %</dd>
        <dt>останется на сделку</dt>
        <dd class="${tone(t.net_bps)}">${signed(t.net_bps, 1)} bps
          = ${signed(t.money, 4)} USDT</dd>`
      : `<dt>останется на сделку</dt>
         <dd class="${tone(d.money_per_trade)}">${signed(g.net_bps, 1)} bps
           = ${signed(d.money_per_trade, 4)} USDT</dd>`}
    </dl>
    <p class="hint" style="margin:10px 0 0">Заложенная доля успеха —
      <b>${fmt(d.input.p * 100, 1)}%</b>${real}. Гейт считает сделку дошедшей
      до цели или до стопа; закрытые по времени платят издержки, но не
      приносят ни того ни другого — поэтому доля тайм-стопов задаётся
      отдельно.</p>`;
}

async function wiRun() {
  const out = $("wi-out");
  if (!out) return;
  try {
    const r = await fetch("/api/whatif?" + wiQuery());
    const d = await r.json();
    if (!r.ok) {
      out.innerHTML = `<div class="conn-bad">✗ ${esc(d.error || ("отказ " + r.status))}</div>`;
      return;
    }
    wiShow(d);
  } catch (err) {
    out.innerHTML = `<div class="conn-bad">✗ панель не ответила: ${esc(err.message)}</div>`;
  }
}

function wiLater() { clearTimeout(wiTimer); wiTimer = setTimeout(wiRun, 300); }

async function wiReset(params) {
  // Значения приходят с сервера, а не из разметки: настройки робота
  // живут в Python, и вторая их копия в HTML рано или поздно отстанет.
  const r = await fetch("/api/whatif" + (params ? "?" + params : ""));
  if (!r.ok) return;
  const d = await r.json();
  wiFill(d.input);
  wiShow(d);
}

function renderWhatIf(s) {
  // Измеренное держим свежим всегда — подставит его кнопка, а не
  // перерисовка: под пальцами числа меняться не должны.
  const c = ((s.paper || {}).quality || {}).counters || {};
  const closed = c.closed || 0;
  const stops = c.reasons || {};
  wiReal = {
    win_rate: closed ? (c.wins || 0) / closed : null,
    timestop: closed
      ? ((stops.time_soft || 0) + (stops.time_hard || 0)) / closed : null,
    spread: ((s.brain || {}).economics || {}).spread_bps,
  };
  if (wiBuilt) return;
  wiBuilt = true;

  $("whatif").innerHTML = `
    <div class="wi-form">
      <label>цель, bps <input type="number" id="wi-tp" min="1" step="1"></label>
      <label>стоп, bps <input type="number" id="wi-sl" min="1" step="1"></label>
      <label>доля успеха, % <input type="number" id="wi-p" min="0" max="100" step="0.1"></label>
      <label>спред, bps <input type="number" id="wi-spread" min="0" step="0.01"></label>
      <label>тайм-стопы, % <input type="number" id="wi-ts" min="0" max="99" step="1"></label>
      <label>размер, USDT <input type="number" id="wi-notional" min="0" step="1"></label>
    </div>
    <div class="wi-row">
      <label class="conn-row" style="gap:7px;font-size:12px;color:var(--ink-2)">
        <input type="checkbox" id="wi-maker"> вход пассивной заявкой</label>
      <span class="spacer"></span>
      <button class="ghost" id="wi-def">Как у робота</button>
      <button class="ghost" id="wi-real">Как измерено</button>
    </div>
    <div id="wi-out"><div class="empty">Считаю…</div></div>`;

  ["wi-tp", "wi-sl", "wi-p", "wi-spread", "wi-ts", "wi-notional"]
    .forEach((id) => { $(id).oninput = wiLater; });
  $("wi-maker").onchange = wiRun;

  $("wi-def").onclick = () => wiReset("");
  $("wi-real").onclick = () => {
    // Подстановка измеренного — весь смысл карточки: те же расчёты,
    // но на числах, которые дал живой поток, а не на предположениях.
    if (!wiReal || wiReal.win_rate === null) return;
    const q = new URLSearchParams();
    q.set("p", String(wiReal.win_rate));
    if (wiReal.timestop !== null) q.set("timestop", String(wiReal.timestop));
    if (wiReal.spread) q.set("spread", String(wiReal.spread * 2));
    wiReset(q.toString());
  };

  wiReset("");
}

/* ── опрос ───────────────────────────────────────────────────── */
let snap = null;          /* последний снимок: им же перерисовывается
                             вкладка при переключении, чтобы график не
                             висел секунду с чужой шириной */

function paint(s) {
  $("sym").textContent = s.symbol;
  renderWallet(s);
  renderStatus(s);
  renderPrice(s);
  renderGates(s.brain);
  renderDetectors(s.brain);
  renderEconomics(s.brain);
  renderBook(s);
  renderTape(s);
  renderTrades(s);
  renderPaper(s);
  renderPaperTrades(s);
  renderWhatIf(s);
  renderNews(s);
  renderAlerts(s);
  renderConn(s);
}

async function tick() {
  try {
    const r = await fetch("/api/state");
    const s = await r.json();
    if (!s.ready) return;
    snap = s;
    paint(s);
    $("upd").textContent = "обновлено " + new Date().toLocaleTimeString("ru-RU");
  } catch (e) {
    $("upd").textContent = "панель не отвечает: " + e.message;
  }
}
tick();
setInterval(tick, 1000);
