// Сборка приложения CashMash для Android.
//
// Проект намеренно отдельный от Python-кода и ничего в нём не меняет:
// приложение — это оболочка, которая даёт роботу интерпретатор, право
// работать в фоне и экран. Логика робота остаётся там, где была, и
// проверяется теми же тестами.

pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // Запасной адрес рантайма Chaquopy: основные артефакты лежат
        // в Maven Central, но сборочные колёса Python берутся отсюда.
        maven("https://chaquo.com/maven")
    }
}

plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    // Chaquopy встраивает настоящий CPython в APK. Альтернативы
    // (Kivy, BeeWare) потребовали бы переписать панель под чужой
    // тулкит; здесь панель остаётся той же веб-страницей, что и на
    // компьютере, и правится в одном месте.
    id("com.chaquo.python") version "17.0.0" apply false
}

rootProject.name = "CashMash"
include(":app")
