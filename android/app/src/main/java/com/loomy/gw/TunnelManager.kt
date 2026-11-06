package com.loomy.gw

import android.content.Context
import android.util.Log
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.net.Inet4Address
import java.net.InetAddress
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/** How the public URL is obtained. */
enum class TunnelMode { QUICK, NAMED }

enum class TunnelStatus { IDLE, STARTING, RUNNING, RECONNECTING, ERROR }

data class TunnelState(
    val status: TunnelStatus = TunnelStatus.IDLE,
    val mode: TunnelMode = TunnelMode.QUICK,
    /** Public URL; for a named tunnel this is the user's bound domain. */
    val url: String? = null,
    val error: String? = null,
    /** Whether the tunnel should come back after a service restart. */
    val enabled: Boolean = false,
)

/**
 * Runs the bundled `cloudflared` binary as a child process and forwards the
 * gateway's loopback port to a public URL.
 *
 * Why a custom binary: the stock Linux build cannot resolve Cloudflare's SRV
 * records on Android (Go's SRV lookup always reads /etc/resolv.conf, which
 * Android does not have). The binary in `jniLibs/arm64-v8a/libcloudflared.so`
 * is cross-compiled with the NDK, and we pass explicit `--edge` addresses
 * resolved through Android's own resolver.
 *
 * The binary is shipped as a `.so` so Android extracts it to `nativeLibraryDir`
 * on install. App-private files are not executable since Android 10.
 */
object TunnelManager {
    private const val TAG = "TunnelManager"

    private const val PREFS = "loomy_tunnel"
    private const val KEY_ENABLED = "enabled"
    private const val KEY_MODE = "mode"
    private const val KEY_TOKEN = "token"
    private const val KEY_DOMAIN = "domain"

    /** cloudflared's edge port; the address itself is resolved per device. */
    private const val EDGE_PORT = 7844
    private val EDGE_HOSTS = listOf(
        "region1.v2.argotunnel.com",
        "region2.v2.argotunnel.com",
    )

    private val QUICK_URL = Regex(
        "https://[a-z0-9-]+\\.trycloudflare\\.com",
        RegexOption.IGNORE_CASE,
    )

    /** Give the process this long to print a URL / register before retrying. */
    private const val START_TIMEOUT_MS = 40_000L
    private const val INITIAL_BACKOFF_MS = 2_000L
    private const val MAX_BACKOFF_MS = 60_000L

    /**
     * Errors that a retry cannot fix: the token itself is unusable. These stop
     * the tunnel immediately and clear the persisted "enabled" flag.
     *
     * Matched against the raw line regardless of level — cloudflared reports
     * some of these at INFO, so an ERR-only filter would miss them ("Provided
     * Tunnel token is not valid." is printed as INF).
     */
    private val FATAL_MARKERS = listOf(
        "Provided Tunnel token is not valid",
        "Invalid tunnel secret",
        "error parsing token",
        "failed to parse token",
    )

    /**
     * Server-side rejections of the credentials. A freshly created tunnel can
     * need a moment to propagate, so a single occurrence is retried — but a
     * wrong/revoked token produces these forever, and retrying that was an
     * endless loop the user could not even switch off. See AUTH_GIVE_UP.
     */
    private val AUTH_MARKERS = listOf(
        "Failed to get tunnel",
        "Unauthorized",
        "unauthorized",
    )

    /** Consecutive credential rejections before the tunnel gives up. */
    private const val AUTH_GIVE_UP = 3

    /**
     * Expected on Android and harmless: cloudflared tries its own DNS
     * resolution / config discovery first. The edge addresses are passed
     * explicitly, so these never prevent a connection.
     */
    private val BENIGN_ERRORS = listOf(
        "Failed to fetch features",
        "Failed to initialize DNS local resolver",
        "Cannot determine default configuration path",
    )

    private val LOG_LEVEL = Regex("\\b(ERR|FTL)\\b")

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val _state = MutableStateFlow(TunnelState())
    val state: StateFlow<TunnelState> = _state.asStateFlow()

    private var process: Process? = null
    private var job: Job? = null
    private var port: Int = GatewayService.PORT
    private val stopRequested = AtomicBoolean(false)

    /**
     * Set by the log reader when cloudflared reports a credential rejection.
     * Consumed by [runLoop] once per attempt to decide when to stop retrying.
     */
    private val authRejected = AtomicBoolean(false)

    // ------------------------------------------------------------ settings

    data class Settings(
        val enabled: Boolean = false,
        val mode: TunnelMode = TunnelMode.QUICK,
        val token: String = "",
        val domain: String = "",
    )

    fun loadSettings(ctx: Context): Settings {
        val sp = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        return Settings(
            enabled = sp.getBoolean(KEY_ENABLED, false),
            mode = if (sp.getString(KEY_MODE, "quick") == "named") {
                TunnelMode.NAMED
            } else {
                TunnelMode.QUICK
            },
            token = sp.getString(KEY_TOKEN, "").orEmpty(),
            domain = sp.getString(KEY_DOMAIN, "").orEmpty(),
        )
    }

    private fun saveSettings(ctx: Context, settings: Settings) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putBoolean(KEY_ENABLED, settings.enabled)
            .putString(KEY_MODE, if (settings.mode == TunnelMode.NAMED) "named" else "quick")
            .putString(KEY_TOKEN, settings.token)
            .putString(KEY_DOMAIN, settings.domain)
            .apply()
    }

    // ----------------------------------------------------------- lifecycle

    /**
     * Start (or restart) the tunnel. Safe to call while one is running: the
     * previous process is killed first.
     */
    fun start(
        ctx: Context,
        mode: TunnelMode,
        token: String,
        domain: String,
        port: Int = GatewayService.PORT,
    ) {
        val app = ctx.applicationContext
        val cleanToken = token.trim()
        val cleanDomain = normalizeDomain(domain)

        // Reject a malformed token before spawning anything: cloudflared would
        // fail with an opaque "Failed to get tunnel" and we would retry it.
        if (mode == TunnelMode.NAMED) {
            val problem = tokenFormatProblem(cleanToken)
            if (problem != null) {
                saveSettings(app, Settings(false, mode, cleanToken, cleanDomain))
                _state.value = TunnelState(
                    TunnelStatus.ERROR, mode, null, problem, enabled = false,
                )
                return
            }
        }

        synchronized(this) {
            stopRequested.set(false)
            job?.cancel()
            job = null
            process?.let { kill(it) }
            process = null

            this.port = port
            saveSettings(app, Settings(true, mode, cleanToken, cleanDomain))
            _state.value = TunnelState(
                TunnelStatus.STARTING, mode, null, null, enabled = true,
            )
            job = scope.launch { runLoop(app, mode, cleanToken, cleanDomain, port) }
        }
    }

    /**
     * Shape check for a named-tunnel token, or null when it looks usable.
     *
     * Cloudflare tokens are base64url JSON with an ``a``/``t``/``s`` payload.
     * Catching an obvious paste error here is much clearer than the upstream's
     * "Failed to get tunnel", and it keeps the process from ever starting.
     */
    private fun tokenFormatProblem(token: String): String? {
        if (token.isBlank()) return "请先填写 Tunnel Token"
        if (token.startsWith("eyJ") && !token.contains('.')) {
            try {
                val decoded = String(
                    android.util.Base64.decode(token,
                        android.util.Base64.URL_SAFE or android.util.Base64.NO_PADDING
                            or android.util.Base64.NO_WRAP),
                    Charsets.UTF_8)
                if (!decoded.contains("\"t\"") || !decoded.contains("\"s\"")) {
                    return "Tunnel Token 内容不完整，请在 Cloudflare 重新复制"
                }
            } catch (_: Exception) {
                return "Tunnel Token 格式不正确，请在 Cloudflare 重新复制"
            }
            return null
        }
        return "Tunnel Token 应以 eyJ 开头，请复制完整的 token"
    }

    /** Stop the tunnel and remember that it is no longer enabled. */
    fun stop(ctx: Context) {
        val app = ctx.applicationContext
        synchronized(this) {
            saveSettings(app, loadSettings(app).copy(enabled = false))
        }
        shutdown()
    }

    /**
     * Stop the process without touching the saved "enabled" flag. Used when the
     * gateway service goes away: a later service start restores the tunnel.
     */
    fun shutdown() {
        synchronized(this) {
            stopRequested.set(true)
            job?.cancel()
            job = null
            process?.let { kill(it) }
            process = null
            _state.value = TunnelState(
                TunnelStatus.IDLE, _state.value.mode, enabled = false,
            )
        }
    }

    /**
     * Re-apply the persisted settings with a new gateway port. Used after the
     * user changes the listening port while a tunnel is up.
     */
    fun restartForPort(ctx: Context, port: Int) {
        val s = loadSettings(ctx)
        if (!s.enabled) return
        start(ctx, s.mode, s.token, s.domain, port)
    }

    /**
     * Update the display domain without restarting the tunnel.
     *
     * Cloudflare's wizard requires the tunnel to connect *before* a Public
     * Hostname can be added, so the domain is often filled in afterwards.
     */
    fun updateDomain(ctx: Context, domain: String) {
        val app = ctx.applicationContext
        val normalized = normalizeDomain(domain)
        synchronized(this) {
            saveSettings(app, loadSettings(app).copy(domain = normalized))
            val current = _state.value
            if (current.mode == TunnelMode.NAMED &&
                (current.status == TunnelStatus.RUNNING ||
                 current.status == TunnelStatus.RECONNECTING)) {
                _state.value = current.copy(url = namedUrl(normalized))
            }
        }
    }

    /** Called when the gateway service (re)starts. */
    fun restoreIfEnabled(ctx: Context, port: Int) {
        val s = loadSettings(ctx)
        if (!s.enabled) return
        // The domain is optional: Cloudflare's setup wizard only needs the
        // tunnel to connect before you can add a Public Hostname.
        if (s.mode == TunnelMode.NAMED && tokenFormatProblem(s.token) != null) {
            saveSettings(ctx, s.copy(enabled = false))
            _state.value = TunnelState(
                TunnelStatus.ERROR, s.mode, null,
                tokenFormatProblem(s.token), enabled = false,
            )
            return
        }
        start(ctx, s.mode, s.token, s.domain, port)
    }

    // ------------------------------------------------------------ internals

    private suspend fun runLoop(
        ctx: Context,
        mode: TunnelMode,
        token: String,
        domain: String,
        port: Int,
    ) {
        var backoff = INITIAL_BACKOFF_MS
        var authFailures = 0
        authRejected.set(false)
        while (!stopRequested.get()) {
            try {
                runOnce(ctx, mode, token, domain, port)
                if (stopRequested.get()) return
                // runOnce only returns when the process exits on its own.
                if (authRejected.getAndSet(false)) {
                    authFailures++
                    if (authFailures >= AUTH_GIVE_UP) {
                        giveUp(ctx, mode,
                               "Tunnel Token 无效或已被吊销，已停止重试")
                        return
                    }
                } else {
                    authFailures = 0
                }
                _state.update {
                    it.copy(
                        status = TunnelStatus.RECONNECTING,
                        error = "隧道已断开，正在重连…",
                    )
                }
            } catch (c: CancellationException) {
                throw c
            } catch (f: TunnelFatal) {
                giveUp(ctx, mode, f.message)
                return
            } catch (e: Exception) {
                if (stopRequested.get()) return
                Log.w(TAG, "tunnel attempt failed", e)
                if (authRejected.getAndSet(false)) {
                    authFailures++
                    if (authFailures >= AUTH_GIVE_UP) {
                        giveUp(ctx, mode,
                               "Tunnel Token 无效或已被吊销，已停止重试")
                        return
                    }
                }
                _state.update {
                    it.copy(
                        status = TunnelStatus.RECONNECTING,
                        error = e.message ?: "启动失败",
                    )
                }
            }
            if (stopRequested.get()) return
            delay(backoff)
            backoff = (backoff * 2).coerceAtMost(MAX_BACKOFF_MS)
        }
    }

    /**
     * Stop for good: record the reason, clear "enabled" so a restart does not
     * resume the same failing loop, and leave the switch operable.
     */
    private fun giveUp(ctx: Context, mode: TunnelMode, message: String?) {
        _state.value = TunnelState(
            TunnelStatus.ERROR, mode, null, message, enabled = false,
        )
        saveSettings(ctx, loadSettings(ctx).copy(enabled = false))
        Log.w(TAG, "tunnel gave up: $message")
    }

    /** Runs one cloudflared process until it exits or a start attempt fails. */
    private suspend fun runOnce(
        ctx: Context,
        mode: TunnelMode,
        token: String,
        domain: String,
        port: Int,
    ) {
        val binary = resolveBinary(ctx)
            ?: throw TunnelFatal("当前设备不支持公网穿透（APK 未包含 arm64 cloudflared）")
        val edges = resolveEdges()
        val args = buildArgs(mode, token, port, edges)

        _state.update { it.copy(status = TunnelStatus.STARTING, url = null, error = null) }
        val p = withContext(Dispatchers.IO) {
            ProcessBuilder(listOf(binary.absolutePath) + args)
                .redirectErrorStream(true)
                .apply {
                    environment()["HOME"] = ctx.filesDir.absolutePath
                    environment()["TMPDIR"] = ctx.cacheDir.absolutePath
                }
                .start()
        }
        // Publish the process under the lock so a concurrent stop()/start()
        // cannot miss it and leave an orphan running.
        synchronized(this) {
            if (stopRequested.get()) {
                kill(p)
                return
            }
            process = p
        }

        // The reader runs in our own scope, so a fatal line cannot be thrown
        // across coroutines — it is recorded here and acted on below.
        val fatal = java.util.concurrent.atomic.AtomicReference<String?>(null)
        val reader = scope.launch {
            try {
                p.inputStream.bufferedReader().useLines { lines ->
                    for (line in lines) {
                        handleLine(line, mode, domain) { message ->
                            fatal.set(message)
                            kill(p)
                        }
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "reader stopped", e)
            }
        }

        try {
            val deadline = System.currentTimeMillis() + START_TIMEOUT_MS
            // When the credentials are rejected, wait only this long for a late
            // success before ending the attempt: a worthless token would
            // otherwise keep the loop waiting for the full start timeout.
            val authGraceMs = 8_000L
            var authSeenAt = 0L
            while (System.currentTimeMillis() < deadline) {
                fatal.get()?.let { throw TunnelFatal(it) }
                if (_state.value.status == TunnelStatus.RUNNING) break
                if (!p.isAlive) break
                if (authRejected.get()) {
                    if (authSeenAt == 0L) {
                        authSeenAt = System.currentTimeMillis()
                    } else if (System.currentTimeMillis() - authSeenAt > authGraceMs) {
                        throw IllegalStateException(
                            _state.value.error ?: "Tunnel Token 被拒绝")
                    }
                }
                delay(250)
            }
            fatal.get()?.let { throw TunnelFatal(it) }
            if (_state.value.status != TunnelStatus.RUNNING) {
                throw IllegalStateException(
                    _state.value.error ?: "启动超时（${START_TIMEOUT_MS / 1000}s 内未获得公网地址）"
                )
            }
            // Wait for the process to exit; the reader keeps state fresh.
            withContext(Dispatchers.IO) { p.waitFor() }
            fatal.get()?.let { throw TunnelFatal(it) }
            reader.join()
        } finally {
            reader.cancel()
            if (p.isAlive) kill(p)
            if (process === p) process = null
        }
    }

    private fun handleLine(
        line: String,
        mode: TunnelMode,
        domain: String,
        onFatal: (String) -> Unit,
    ) {
        Log.i(TAG, line)
        if (mode == TunnelMode.QUICK) {
            QUICK_URL.find(line)?.let { m ->
                _state.update {
                    it.copy(status = TunnelStatus.RUNNING, url = m.value, error = null)
                }
            }
        } else if (line.contains("Registered tunnel connection")) {
            _state.update {
                it.copy(status = TunnelStatus.RUNNING, url = namedUrl(domain), error = null)
            }
        }

        // Checked before the level filter: cloudflared logs this at INFO.
        if (FATAL_MARKERS.any { line.contains(it) }) {
            onFatal("Tunnel Token 无效，请在 Cloudflare 重新复制")
            return
        }

        if (!LOG_LEVEL.containsMatchIn(line)) return
        val message = line.substringAfter("ERR", line).substringAfter("FTL", line)
            .trim().take(240)
        if (FATAL_MARKERS.any { line.contains(it) }) {
            onFatal(message)
            return
        }
        if (AUTH_MARKERS.any { line.contains(it) }) {
            // Counted by runLoop; the process is left to exit on its own so the
            // attempt boundary (and the backoff) stays meaningful.
            authRejected.set(true)
            _state.update { it.copy(error = message) }
            return
        }
        if (BENIGN_ERRORS.any { line.contains(it) }) return
        _state.update {
            // Do not overwrite the URL once connected with a later warning.
            if (it.status == TunnelStatus.RUNNING) it else it.copy(error = message)
        }
    }

    private fun buildArgs(
        mode: TunnelMode,
        token: String,
        port: Int,
        edges: List<String>,
    ): List<String> {
        val args = mutableListOf("tunnel", "--no-autoupdate", "--protocol", "http2")
        edges.forEach { args += listOf("--edge", it) }
        when (mode) {
            TunnelMode.QUICK -> args += listOf("--url", "http://127.0.0.1:$port")
            TunnelMode.NAMED -> {
                args += "run"
                args += listOf("--token", token)
            }
        }
        return args
    }

    /**
     * Resolve Cloudflare edge hosts through Android's resolver.
     *
     * Passing IPs avoids cloudflared's SRV lookup, which always uses the Go
     * resolver and fails on Android (no /etc/resolv.conf). An empty result is
     * not fatal: on other devices the hostname form may work.
     */
    private fun resolveEdges(): List<String> {
        val addresses = LinkedHashSet<String>()
        for (host in EDGE_HOSTS) {
            try {
                InetAddress.getAllByName(host)
                    .filterIsInstance<Inet4Address>()
                    .take(2)
                    .forEach { addresses += "${it.hostAddress}:$EDGE_PORT" }
            } catch (e: Exception) {
                Log.w(TAG, "edge resolve failed for $host: ${e.message}")
            }
        }
        return addresses.take(4)
    }

    private fun resolveBinary(ctx: Context): File? {
        val file = File(ctx.applicationInfo.nativeLibraryDir, "libcloudflared.so")
        return file.takeIf { it.isFile && it.canExecute() }
    }

    private fun normalizeDomain(input: String): String =
        input.trim()
            .removePrefix("https://")
            .removePrefix("http://")
            .trimEnd('/')

    private fun namedUrl(domain: String): String? =
        domain.ifBlank { null }?.let { "https://$it" }

    private fun kill(p: Process) {
        try {
            p.destroy()
            if (!p.waitFor(2, TimeUnit.SECONDS)) p.destroyForcibly()
        } catch (_: Exception) {
            p.destroyForcibly()
        }
    }

    private class TunnelFatal(message: String?) : Exception(message)
}
