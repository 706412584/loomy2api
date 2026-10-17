package com.loomy.gw

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/**
 * Update check against the project's GitHub releases.
 *
 * The repository is private, so the release API needs a token.  It is *not*
 * baked into the APK (that would leak it to anyone who unzips it); the user
 * pastes a fine-grained token in Settings and it is stored in app-private
 * storage.  Without a token the check simply reports "not configured".
 */
object Updater {
    /** Release assets are named `loomy2api-<flavor>-<version>.apk`. */
    private const val API = "https://api.github.com/repos/706412584/loomy2api/releases/latest"

    data class Release(
        val tag: String,
        val notes: String,
        /** GitHub API asset URL (needs the token) — redirects to a signed URL. */
        val apkUrl: String,
        val apkName: String,
    )

    fun token(ctx: Context): String =
        runCatching { File(ctx.filesDir, "github_token").readText().trim() }
            .getOrDefault("")

    fun saveToken(ctx: Context, token: String) {
        runCatching {
            val f = File(ctx.filesDir, "github_token")
            if (token.isBlank()) f.delete() else f.writeText(token.trim())
        }
    }

    /** Which asset belongs to this build: `full` or `lite`. */
    private fun flavor(): String =
        if (BuildConfig.ENABLE_SMS_REGISTRATION) "full" else "lite"

    /**
     * Compare the newest release with the running version.
     * Returns the release only when it is newer, else null.
     */
    suspend fun check(ctx: Context): Pair<Release?, String> = withContext(Dispatchers.IO) {
        val token = token(ctx)
        if (token.isBlank()) return@withContext null to "未配置 GitHub Token"
        val conn = (URL(API).openConnection() as HttpURLConnection).apply {
            connectTimeout = 10_000
            readTimeout = 15_000
            setRequestProperty("Authorization", "Bearer $token")
            setRequestProperty("Accept", "application/vnd.github+json")
            setRequestProperty("User-Agent", "loomy2api-android")
        }
        try {
            if (conn.responseCode != 200) {
                return@withContext null to "检查失败：HTTP ${conn.responseCode}"
            }
            val body = conn.inputStream.bufferedReader().use { it.readText() }
            val obj = JSONObject(body)
            val tag = obj.optString("tag_name").trim()
            val latest = tag.trimStart('v').split('.').mapNotNull { it.toIntOrNull() }
            val current = BuildConfig.VERSION_NAME
                .substringBefore('-').split('.').mapNotNull { it.toIntOrNull() }
            if (!isNewer(latest, current)) {
                return@withContext null to "已是最新版本 ${BuildConfig.VERSION_NAME}"
            }
            val assets = obj.optJSONArray("assets") ?: return@withContext null to "该版本没有安装包"
            val wanted = flavor()
            var url = ""
            var name = ""
            for (i in 0 until assets.length()) {
                val a = assets.getJSONObject(i)
                val n = a.optString("name")
                if (n.startsWith("loomy2api-$wanted") && n.endsWith(".apk")) {
                    // The repo is private: browser_download_url needs a session
                    // cookie, while the API asset URL accepts the bearer token.
                    url = a.optString("url")
                    name = n
                    break
                }
            }
            if (url.isEmpty()) {
                return@withContext null to "该版本没有 $wanted 安装包"
            }
            Release(tag, obj.optString("body"), url, name) to "发现新版本 $tag"
        } catch (e: Exception) {
            null to "检查失败：${e.message ?: e.javaClass.simpleName}"
        } finally {
            conn.disconnect()
        }
    }

    private fun isNewer(latest: List<Int>, current: List<Int>): Boolean {
        val n = maxOf(latest.size, current.size)
        for (i in 0 until n) {
            val a = latest.getOrElse(i) { 0 }
            val b = current.getOrElse(i) { 0 }
            if (a != b) return a > b
        }
        return false
    }

    /** Download the APK into cache and hand the installer an opaque Uri. */
    suspend fun download(ctx: Context, release: Release,
                         onProgress: (Int) -> Unit = {}): Pair<File?, String> =
        withContext(Dispatchers.IO) {
            val token = token(ctx)
            val dir = File(ctx.cacheDir, "updates").apply { mkdirs() }
            val out = File(dir, release.apkName)
            // The asset API answers 302 to a short-lived signed URL.  Do not
            // forward the token there — and do follow the redirect.
            val conn = (URL(release.apkUrl).openConnection() as HttpURLConnection).apply {
                instanceFollowRedirects = true
                connectTimeout = 15_000
                readTimeout = 60_000
                setRequestProperty("Authorization", "Bearer $token")
                setRequestProperty("Accept", "application/octet-stream")
                setRequestProperty("User-Agent", "loomy2api-android")
            }
            try {
                if (conn.responseCode != 200) {
                    return@withContext null to "下载失败：HTTP ${conn.responseCode}"
                }
                val total = conn.contentLength
                conn.inputStream.use { input ->
                    out.outputStream().use { output ->
                        val buf = ByteArray(64 * 1024)
                        var done = 0
                        while (true) {
                            val n = input.read(buf)
                            if (n <= 0) break
                            output.write(buf, 0, n)
                            done += n
                            if (total > 0) onProgress(done * 100 / total)
                        }
                    }
                }
                out to "已下载 ${out.length() / 1024 / 1024} MB"
            } catch (e: Exception) {
                null to "下载失败：${e.message ?: e.javaClass.simpleName}"
            } finally {
                conn.disconnect()
            }
        }

    /**
     * Open the system installer.  Needs the REQUEST_INSTALL_PACKAGES permission
     * and, on Android 8+, the user's per-app "install unknown apps" toggle.
     */
    fun install(ctx: Context, apk: File): String {
        if (Build.VERSION.SDK_INT >= 26 && !ctx.packageManager.canRequestPackageInstalls()) {
            // send the user straight to the toggle instead of failing silently
            runCatching {
                ctx.startActivity(Intent(
                    android.provider.Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:${ctx.packageName}")).apply {
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                })
            }
            return "请在「安装未知应用」里允许本应用，然后再次点击更新"
        }
        val uri = FileProvider.getUriForFile(ctx, "${ctx.packageName}.updates", apk)
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        return try {
            ctx.startActivity(intent)
            "正在打开安装器…"
        } catch (e: Exception) {
            "无法打开安装器：${e.message}"
        }
    }
}
