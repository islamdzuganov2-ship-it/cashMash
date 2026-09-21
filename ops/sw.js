/* Сервис-воркер панели CashMash.
 *
 * Делает ровно две вещи и ни одной лишней.
 *
 * 1. ОБОЛОЧКА ИЗ КЭША. Разметка, скрипт и иконки кладутся в кэш при
 *    установке, и дальше отдаются оттуда. На телефоне это разница
 *    между «приложение открылось» и «белый экран, пока грузится».
 *
 * 2. ДАННЫЕ — ТОЛЬКО ИЗ СЕТИ. `/api/state` НИКОГДА не кэшируется.
 *    Устаревшее состояние счёта хуже отсутствующего: цена двухминутной
 *    давности выглядит как текущая, и по ней принимают решения.
 *    При потере связи возвращается честный признак offline, а панель
 *    показывает «не отвечает» — это правда, а не ошибка.
 *
 * Оговорка про среду. Сервис-воркеры работают только в защищённом
 * контексте: localhost или HTTPS. Панель, открытая с телефона по
 * локальной сети через обычный HTTP, воркер НЕ зарегистрирует — и это
 * нормально: установка на домашний экран, полноэкранный режим и иконка
 * работают и без него, потому что за них отвечает манифест.
 */

const VERSION = "cashmash-v1";
const SHELL = [
  "/",
  "/app.js",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/apple-touch-icon.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(VERSION)
      .then((c) => c.addAll(SHELL))
      // Не падаем, если какой-то файл недоступен: оболочка без одной
      // иконки работает, а провал установки лишает воркера целиком.
      .catch(() => undefined)
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin) return;

  // Состояние — только из сети. Кэшировать его нельзя ни при каких
  // условиях: см. заголовок файла.
  if (url.pathname.startsWith("/api/")) {
    e.respondWith(
      fetch(e.request).catch(() => new Response(
        JSON.stringify({ ready: false, offline: true }),
        { status: 503, headers: { "Content-Type": "application/json" } }))
    );
    return;
  }

  // Оболочка: сначала сеть (чтобы правки были видны сразу), кэш —
  // запасной вариант. Обратный порядок заставлял бы чистить кэш после
  // каждой правки вёрстки.
  e.respondWith(
    fetch(e.request)
      .then((r) => {
        const copy = r.clone();
        caches.open(VERSION).then((c) => c.put(e.request, copy)).catch(() => {});
        return r;
      })
      .catch(() => caches.match(e.request).then((r) => r || Response.error()))
  );
});
