import java.security.MessageDigest
import java.util.Properties
import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// Корень Python-проекта: приложение лежит в android/, робот — этажом выше.
val robotRoot: File = rootProject.projectDir.parentFile

/**
 * Python для сборки.
 *
 * Chaquopy ставит колёса под Android настоящим pip, а его нужно чем-то
 * запустить — интерпретатором ТОЙ ЖЕ младшей версии, что и целевая:
 * иначе pip подберёт колёса не под тот ABI.
 *
 * Путь берётся из local.properties (`chaquopy.buildPython=...`), а не
 * вписывается сюда: у каждой машины он свой, а файл сборки один на всех.
 * Если не задан — Chaquopy ищет `python` сам, как и раньше.
 */
val buildPythonPath: String? = rootProject.file("local.properties")
    .takeIf { it.isFile }
    ?.let { file ->
        Properties().apply { file.inputStream().use { stream -> load(stream) } }
            .getProperty("chaquopy.buildPython")
    }
    ?.takeIf { path -> path.isNotBlank() }

/** Всё из local.properties — файла, которого нет в репозитории. */
val localProps: Properties = Properties().apply {
    val file = rootProject.file("local.properties")
    if (file.isFile) file.inputStream().use { stream -> load(stream) }
}

android {
    namespace = "com.cashmash.robot"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.cashmash.robot"
        // 26 — первая версия с каналами уведомлений. Ниже опускаться
        // незачем: робот, которого нельзя показать в уведомлении,
        // будет убит системой через несколько минут.
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"

        ndk {
            // Python 3.12 в Chaquopy существует только для 64 бит.
            // 32-битных телефонов, на которых имеет смысл держать
            // круглосуточный процесс, всё равно не осталось.
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    // Ключ подписи создаёт человек, а не сборка, и пароль к нему не
    // хранится в репозитории. Пути и пароли читаются из local.properties;
    // нет их — release просто не подписывается, и это видно сразу, а не
    // после установки.
    //
    //   keytool -genkeypair -v -keystore cashmash.jks -alias cashmash \
    //           -keyalg RSA -keysize 4096 -validity 10000
    //
    // Ключ нельзя терять: обновление приложения принимается, только если
    // подписано тем же ключом.
    signingConfigs {
        val store = localProps.getProperty("release.keystore")
        if (!store.isNullOrBlank() && file(store).isFile) {
            create("release") {
                storeFile = file(store)
                storePassword = localProps.getProperty("release.storePassword")
                keyAlias = localProps.getProperty("release.keyAlias")
                keyPassword = localProps.getProperty("release.keyPassword")
            }
        }
    }

    buildTypes {
        release {
            // R8 выключен намеренно. Chaquopy связывает Python и Java
            // через рефлексию, и проверить, что сокращение ничего не
            // сломало, можно только запуском на устройстве. Выигрыш
            // в размере здесь дают разделённые ABI, а не минификация,
            // поэтому риск не окупается.
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"),
                          "proguard-rules.pro")
            signingConfig = signingConfigs.findByName("release")
        }
        debug {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
        }
    }

    // По APK на архитектуру вместо одного на обе. Телефону нужен ровно
    // один набор нативных библиотек, а Python с зависимостями — это
    // почти весь вес: общий файл тащит на устройство вдвое больше, чем
    // там когда-либо исполнится.
    splits {
        abi {
            isEnable = true
            reset()
            include("arm64-v8a", "x86_64")
            // Универсальный APK всё же собирается: он нужен, когда
            // ставят вручную и не знают архитектуру телефона.
            isUniversalApk = true
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        viewBinding = true
        buildConfig = true
    }

    packaging {
        resources.excludes += setOf("META-INF/*.kotlin_module")
    }
}

kotlin {
    compilerOptions { jvmTarget.set(JvmTarget.JVM_17) }
}

chaquopy {
    defaultConfig {
        // Та же версия, на которой робот разрабатывается и проходит
        // тесты. Расхождение младших версий между телефоном и машиной
        // разработчика означало бы, что «проверено» и «работает» —
        // про разные интерпретаторы.
        version = "3.13"
        buildPythonPath?.let { buildPython(it) }

        pip {
            // Только то, без чего службы не запускаются. Каждое колесо —
            // это мегабайты в APK и риск, что под Android его нет:
            // pydantic, например, не ставится вообще (ядро на Rust),
            // и поэтому конфиг умеет обходиться без него.
            install("websockets>=12")
            install("requests>=2.31")
            install("PyYAML>=6")
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
}

// --- упаковка Python-проекта в ресурсы приложения ----------------------
//
// Почему проект едет ассетом, а не исходниками Chaquopy. Chaquopy кладёт
// свой каталог исходников в ZIP внутри APK и распаковывает оттуда только
// модули. Роботу этого мало: панель читает dashboard.html и иконки как
// обычные файлы, а службы пишут рядом с собой. Проще и честнее отдать
// приложению дерево целиком и распаковать его в рабочий каталог — там
// оно ведёт себя ровно так же, как на компьютере.
val packRobot = tasks.register<Sync>("packRobot") {
    description = "Собрать дерево робота для распаковки на телефоне"
    into(layout.buildDirectory.dir("generated/robot/project"))

    from(robotRoot) {
        include("src/**")
        include("ops/**")
        include("config/**")
        include("research/collect_bybit.py")

        // Секреты в APK не попадают НИКОГДА. ops/.env — это токен
        // Telegram и, возможно, ключи биржи; APK же копируется,
        // пересылается и живёт в загрузках. На телефоне файл создаётся
        // заново, из настроек приложения.
        exclude("ops/.env")
        exclude("**/.env")
        exclude("**/*.pem", "**/*.key")

        // Файлы с точки в начале упаковщик Android выбрасывает молча
        // (правило `.*` в его списке игнорирования). Если оставить их
        // здесь, они попадут в опись, но не в APK, и распаковка упадёт
        // на файле, которого нет. Образец ops/.env.example телефону и
        // не нужен: сам .env пишется из настроек приложения.
        exclude("**/.*")

        exclude("**/__pycache__/**", "**/*.pyc")
        exclude("**/.mypy_cache/**", "**/.pytest_cache/**")
        // Данные робота — это гигабайты истории. На телефоне он наживёт
        // свои.
        exclude("data/**", "**/reports/**", "**/runs/**")
    }

    doLast {
        // Опись нужна распаковщику: AssetManager умеет перечислять
        // каталог, но делает это рекурсивным обходом с сотнями вызовов
        // через JNI — на старте приложения это заметная пауза.
        val root = layout.buildDirectory.dir("generated/robot/project").get().asFile
        val lines = root.walkTopDown()
            .filter { it.isFile }
            .map { it.relativeTo(root).invariantSeparatorsPath }
            .sorted()
            .toList()
        // Опись обязана совпадать с тем, что реально ляжет в APK.
        // Упаковщик Android выбрасывает файлы с точки и каталоги с
        // подчёркивания, ничего об этом не сообщая; расхождение вскроется
        // только на телефоне, при распаковке, и будет выглядеть как
        // «робот не запускается» без связи с причиной.
        val dropped = lines.filter { line ->
            line.split("/").any { part -> part.startsWith(".") } ||
                line.split("/").dropLast(1).any { part -> part.startsWith("_") }
        }
        if (dropped.isNotEmpty()) {
            error("packRobot: эти файлы не переживут упаковку и сломают " +
                  "распаковку на телефоне — исключите их: $dropped")
        }

        val manifest = File(root.parentFile, "project-files.txt")
        manifest.writeText(lines.joinToString("\n"))

        // Отпечаток СОДЕРЖИМОГО, а не версии приложения.
        //
        // Сначала здесь стояли versionCode и versionName, и это был
        // тихий дефект: правка в Python попадала в APK, но на телефоне
        // распаковка её не замечала — версия-то прежняя. Приложение
        // обновлялось, а робот продолжал работать по старому коду, и
        // понять это было нельзя ничем, кроме сравнения файлов вручную.
        val digest = MessageDigest.getInstance("SHA-256")
        for (name in lines) {
            digest.update(name.toByteArray())
            digest.update(File(root, name).readBytes())
        }
        val stamp = digest.digest().joinToString("") { byte -> "%02x".format(byte) }
        File(root.parentFile, "project-stamp.txt").writeText(stamp)

        logger.lifecycle("packRobot: ${lines.size} файлов робота, " +
                         "отпечаток ${stamp.take(12)}")
    }
}

android.sourceSets["main"].assets.srcDir(layout.buildDirectory.dir("generated/robot"))

tasks.named("preBuild") { dependsOn(packRobot) }

