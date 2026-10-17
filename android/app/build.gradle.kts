plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("com.chaquo.python")
}


android {
    namespace = "com.loomy.gw"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.loomy.gw"
        minSdk = 26
        targetSdk = 34
        versionCode = 20
        versionName = "2.3.0"

        ndk {
            // Chaquopy needs abiFilters; arm64 covers modern phones
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    // The keystore is deliberately NOT in git (it would let anyone sign
    // updates).  Provide it locally, or override the passwords via
    // -PloomyStorePassword=… / env vars.  Without it the release build falls
    // back to the debug signature so `assembleRelease` still works.
    val keystore = rootProject.file("loomy-release.jks")
    signingConfigs {
        if (keystore.exists()) {
            create("release") {
                storeFile = keystore
                storePassword = (findProperty("loomyStorePassword") as String?)
                    ?: System.getenv("LOOMY_STORE_PASSWORD") ?: "loomy2api2026"
                keyAlias = (findProperty("loomyKeyAlias") as String?) ?: "loomy"
                keyPassword = (findProperty("loomyKeyPassword") as String?)
                    ?: System.getenv("LOOMY_KEY_PASSWORD") ?: "loomy2api2026"
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            if (keystore.exists()) {
                signingConfig = signingConfigs.getByName("release")
            }
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    // Pull the gateway package into the APK under assets/loomy2api/.
    // The source of truth is the repository root (../loomy2api), so the
    // Android build always ships exactly the Python that is under version
    // control — no hand-maintained copy to drift out of sync.
    sourceSets {
        getByName("main") {
            assets.srcDir(rootProject.file("../loomy2api-pkg"))
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        compose = true
    }
    packaging {
        resources.excludes += setOf("META-INF/*.kotlin_module")
    }
}

// ---------------------------------------------------------------------------
// Sync the Python gateway from the repository root into the packaged assets.
//
// The repo is the single source of truth: `../loomy2api` (and its web/ dir) is
// copied into `../loomy2api-pkg/loomy2api` on every build, so the APK can never
// ship a stale copy.  `unpacked.version` is restamped each time so the app
// re-extracts the package after an upgrade (see mobile_main.start_gateway).
// ---------------------------------------------------------------------------
val pkgRoot = rootProject.file("../loomy2api-pkg")
val pySource = rootProject.file("../loomy2api")

val syncPythonPackage = tasks.register<Sync>("syncPythonPackage") {
    // the package directory already contains web/index.html
    from(pySource) {
        exclude("__pycache__/**", "**/*.pyc")
    }
    into(pkgRoot.resolve("loomy2api"))
    outputs.upToDateWhen { false }          // sources are small; always refresh
}

val stampAssetVersion = tasks.register("stampAssetVersion") {
    val out = pkgRoot.resolve("loomy2api/unpacked.version")
    outputs.upToDateWhen { false }
    outputs.file(out)
    doLast {
        out.parentFile.mkdirs()
        out.writeText(System.currentTimeMillis().toString())
    }
}
stampAssetVersion.configure { dependsOn(syncPythonPackage) }

tasks.matching {
    it.name.matches(Regex("(merge.*Assets|lintVitalAnalyze.*|preBuild.*)"))
}.configureEach {
    dependsOn(stampAssetVersion)
}

chaquopy {
    defaultConfig {
        version = "3.10"   // matches the local CPython used to compile bytecode
        buildPython("C:/Users/70641/AppData/Local/Programs/Python/Python310/python.exe")
    }
}

dependencies {
    // NOTE: the Chaquopy plugin injects com.chaquo.python.runtime:runtime:<ver>
    // (with classifier) itself — do not declare it manually, the coordinate
    // lives under com/chaquo/python/runtime/chaquopy/ on Maven Central.

    // Jetpack Compose — the mobile UI is native (the WebView panel remains
    // available for the desktop gateway and as a fallback on the phone).
    implementation(platform("androidx.compose:compose-bom:2024.10.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    debugImplementation("androidx.compose.ui:ui-tooling")
}
