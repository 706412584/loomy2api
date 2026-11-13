package com.loomy.gw

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

// ===================================================================== state

data class UiState(
    val loading: Boolean = true,
    val ready: Boolean = false,
    val state: StateView? = null,
    val models: List<ModelView> = emptyList(),
    val records: List<CallRecord> = emptyList(),
    val recordTotals: JSONObject = JSONObject(),
    val logLines: List<String> = emptyList(),
    val logPath: String = "",
    val error: String? = null,
    val toast: String? = null,
    val batchRunning: Boolean = false,
    val batchMsg: String = "",
)

class GatewayViewModel : ViewModel() {
    private val _ui = MutableStateFlow(UiState())
    val ui: StateFlow<UiState> = _ui.asStateFlow()

    /** Live Cloudflare tunnel state (owned by the foreground service). */
    val tunnel: StateFlow<TunnelState> = TunnelManager.state

    /**
     * Start the public tunnel. If the gateway has no API key yet, one is
     * generated and saved first: a public tunnel without auth exposes both the
     * model API and the panel endpoints to anyone with the URL.
     */
    fun startTunnel(mode: TunnelMode, token: String, domain: String) {
        val ctx = AssetBridge.appContext ?: return
        viewModelScope.launch(crashGuard) {
            // Do not decide on a stale/absent snapshot: right after a restart
            // the first state read may still be in flight, and generating a key
            // then would silently replace a key the user already has.
            if (_ui.value.state == null) {
                runCatching { loadState() }
                    .onFailure {
                        toast("读取网关配置失败：${it.message}")
                        return@launch
                    }
            }
            val existing = _ui.value.state?.config?.apiKeys.orEmpty()
            if (existing.isEmpty()) {
                val key = "sk-" + java.math.BigInteger(
                    1, java.security.SecureRandom().generateSeed(24)
                ).toString(36).take(32)
                val body = JSONObject().put("api_keys", JSONArray(listOf(key)))
                runCatching { Api.post("/api/panel/config", body) }
                    .onSuccess {
                        Api.apiKey = key
                        loadState()
                        toast("已自动生成 API Key：$key")
                    }
                    .onFailure {
                        toast("生成 API Key 失败：${it.message}")
                        return@launch
                    }
            }
            TunnelManager.start(ctx, mode, token, domain, GatewayService.PORT)
        }
    }

    fun stopTunnel() {
        AssetBridge.appContext?.let { TunnelManager.stop(it) }
    }

    /** Apply a domain edited while the named tunnel is already running. */
    fun updateTunnelDomain(domain: String) {
        AssetBridge.appContext?.let { TunnelManager.updateDomain(it, domain) }
    }

    /** Release found by the last update check, ready to download. */
    @Volatile private var pendingRelease: Updater.Release? = null

    /**
     * Last line of defence.  A gateway that answers an unexpected status (e.g.
     * HTTP 501 from a half-dead server) used to surface as an uncaught
     * `ApiException` on the main dispatcher and killed the app.  Every launch
     * below is individually guarded, but a coroutine can still fail while
     * resuming; this handler turns that into a toast instead of a crash.
     */
    private val crashGuard = kotlinx.coroutines.CoroutineExceptionHandler { _, t ->
        android.util.Log.e("GatewayViewModel", "uncaught coroutine failure", t)
        _ui.update { it.copy(loading = false, toast = "操作失败：${t.message ?: t.javaClass.simpleName}") }
    }

    init {
        AssetBridge.appContext?.let { Api.loadStoredKey(it) }
        // A key configured by another entry point (CLI, desktop gateway, or an
        // older build) is not in SharedPreferences; the gateway process knows
        // the real value, and the panel endpoints cannot tell us without it.
        viewModelScope.launch(crashGuard) {
            val ctx = AssetBridge.appContext ?: return@launch
            if (runCatching { Api.waitUntilReady() }.getOrDefault(false)) {
                val key = withContext(Dispatchers.IO) { GatewayService.readApiKey(ctx) }
                if (key.isNotEmpty() && key != Api.apiKey) Api.apiKey = key
            }
        }
        viewModelScope.launch(crashGuard) {
            val ok = runCatching { Api.waitUntilReady() }.getOrDefault(false)
            _ui.update { it.copy(ready = ok, loading = false,
                error = if (ok) null else "网关未启动（请点首页的启动按钮）") }
            if (ok) refreshAll()
        }
        // keep the dashboard live while the screen is open
        viewModelScope.launch(crashGuard) {
            while (true) {
                delay(10_000)
                if (_ui.value.ready) runCatching { loadState() }
            }
        }
    }

    fun startGateway() {
        _ui.update { it.copy(loading = true, error = null) }
        GatewayService.start(AssetBridge.appContext!!)
        viewModelScope.launch(crashGuard) {
            val ok = runCatching { Api.waitUntilReady() }.getOrDefault(false)
            _ui.update { it.copy(loading = false, ready = ok) }
            if (ok) refreshAll()
        }
    }

    /**
     * Ask the service to restart the Python HTTP server.  Used when the UI sees
     * a stuck gateway; the watchdog normally does this on its own.
     */
    fun restartGateway() {
        _ui.update { it.copy(loading = true, error = null) }
        viewModelScope.launch(crashGuard) {
            val ctx = AssetBridge.appContext
            if (ctx != null) {
                GatewayService.start(ctx)
                runCatching {
                    // the service exposes the same call the watchdog uses
                    GatewayService.instance?.let {
                        com.chaquo.python.Python.getInstance()
                            .getModule("mobile_main").callAttr("restart_gateway")
                    }
                }
            }
            val ok = runCatching { Api.waitUntilReady(60_000) }.getOrDefault(false)
            _ui.update { it.copy(loading = false, ready = ok) }
            if (ok) {
                toast("网关已重启")
                refreshAll()
            } else {
                toast("重启超时，请查看运行日志")
            }
        }
    }

    fun refreshAll() {
        viewModelScope.launch(crashGuard) {
            _ui.update { it.copy(loading = true) }
            val errors = mutableListOf<String>()
            runCatching { loadState() }.onFailure { errors += "状态: ${it.message}" }
            runCatching { loadModels() }.onFailure { errors += "模型: ${it.message}" }
            runCatching { loadRecords() }.onFailure { errors += "记录: ${it.message}" }
            _ui.update { it.copy(
                loading = false,
                error = errors.firstOrNull(),
            ) }
        }
    }

    private suspend fun loadState(refresh: Boolean = false) {
        // `async=1`: the gateway returns cached quota immediately and refreshes
        // in the background. Waiting for every account's round-trip is what
        // made the home screen sit on "starting" for a long time with a large
        // pool. An explicit refresh still asks for fresh values.
        val query = when {
            refresh -> "?refresh=1"
            else -> "?async=1"
        }
        val s = parseState(Api.get("/api/panel/state$query"))
        // keep the client key in sync with the gateway: if the user set keys
        // in the settings tab, every later call needs them (and vice versa)
        val key = s.config.apiKeys.firstOrNull().orEmpty()
        if (Api.apiKey != key) Api.apiKey = key
        _ui.update { it.copy(state = s, ready = true) }
    }

    private suspend fun loadModels(refresh: Boolean = false) {
        val m = parseModels(Api.get("/api/panel/models" + if (refresh) "?refresh=1" else ""))
        _ui.update { it.copy(models = m) }
    }

    private suspend fun loadRecords() {
        val (rows, totals) = parseRecords(Api.get("/api/panel/records?limit=200"))
        _ui.update { it.copy(records = rows, recordTotals = totals) }
    }

    fun refreshQuota() = viewModelScope.launch(crashGuard) {
        toast("正在刷新额度…")
        // many accounts × 30 s upstream timeout — the default 120 s is too tight
        runCatching { Api.post("/api/panel/refresh", timeoutSec = 300) }
            .onSuccess { loadState(refresh = true); toast("额度已刷新") }
            .onFailure { toast("刷新失败：${it.message}") }
    }

    fun refreshModels() = viewModelScope.launch(crashGuard) {
        toast("正在刷新模型…")
        runCatching { loadModels(refresh = true); toast("模型已刷新") }
            .onFailure { toast("刷新失败：${it.message}") }
    }

    /**
     * Health-check the pool and repair what is broken ("检测续期").
     *
     * Calls the same endpoint as the web panel but deliberately never forces
     * a full re-login: every forced login invalidates the session on the
     * account's other devices ("顶号"), which is exactly what left half the
     * pool dead earlier.  Only accounts with a missing/expired/rejected
     * session are re-logged in.
     */
    fun checkPool() = viewModelScope.launch(crashGuard) {
        _ui.update { it.copy(batchRunning = true, batchMsg = "检测续期：检查中…") }
        try {
            val r = Api.post("/api/panel/accounts/check", JSONObject(),
                timeoutSec = 600)
            if (!r.optBoolean("ok", true)) {
                throw IllegalStateException(r.optString("error").ifBlank { "检测失败" })
            }
            val total = r.optInt("total", 0)
            val healthy = r.optInt("healthy", 0)
            val repaired = r.optInt("repaired", 0)
            val failed = r.optInt("failed", 0)
            val bad = mutableListOf<String>()
            val results = r.optJSONArray("results") ?: JSONArray()
            for (i in 0 until results.length()) {
                val row = results.getJSONObject(i)
                if (!row.optBoolean("ok", true)) {
                    bad += row.optString("name") + "：" +
                        row.optString("error").ifBlank { "未知错误" }
                }
            }
            var msg = "检测完成：正常 $healthy/$total"
            if (repaired > 0) msg += "，重登修复 $repaired"
            if (failed > 0) msg += "，仍有 $failed 个异常"
            if (bad.isNotEmpty()) msg += "\n" + bad.take(5).joinToString("\n")
            _ui.update { it.copy(batchMsg = msg) }
            loadState(refresh = true)
        } catch (e: Exception) {
            _ui.update {
                it.copy(batchMsg = "检测续期失败：${e.message ?: e.javaClass.simpleName}")
            }
        } finally {
            _ui.update { it.copy(batchRunning = false) }
        }
    }

    /**
     * Run [action] for every usable account, a few at a time.
     *
     * Accounts are independent, so a small amount of concurrency cuts wall
     * time dramatically (the upstream tolerates it — the official client does
     * the same).  A transient 5xx is retried once after a short pause; 4xx
     * responses are reported as-is because retrying cannot help.
     */
    private fun runBatch(label: String, action: suspend (AccountView) -> String) =
        viewModelScope.launch(crashGuard) {
            val accounts = _ui.value.state?.accounts.orEmpty().filter { it.healthy }
            if (accounts.isEmpty()) {
                _ui.update { it.copy(batchMsg = "没有可用账号") }
                return@launch
            }
            _ui.update { it.copy(batchRunning = true,
                batchMsg = "${label}：0/${accounts.size}") }

            var done = 0
            var okCount = 0
            val failures = mutableListOf<String>()
            val lock = Any()

            suspend fun runOne(acc: AccountView): String {
                var result = runCatching { action(acc) }
                    .getOrElse { "失败：${it.message}" }
                // one retry for transient upstream failures (5xx / timeout)
                val transient = result.startsWith("失败") &&
                    (result.contains("5") || result.contains("超时") ||
                     result.contains("timeout", true))
                if (transient) {
                    kotlinx.coroutines.delay(800)
                    result = runCatching { action(acc) }
                        .getOrElse { "失败：${it.message}" }
                }
                return result
            }

            kotlinx.coroutines.coroutineScope {
                accounts.chunked(3).forEach { group ->
                    group.map { acc ->
                        async {
                            val result = runOne(acc)
                            synchronized(lock) {
                                done++
                                if (result.startsWith("失败")) {
                                    failures += "${acc.name}: $result"
                                } else {
                                    okCount++
                                }
                                _ui.value = _ui.value.copy(
                                    batchMsg = "${label}：$done/${accounts.size}"
                                        + (if (failures.isNotEmpty())
                                            " · 失败 ${failures.size}" else ""))
                            }
                        }
                    }.awaitAll()
                }
            }

            _ui.update {
                it.copy(
                    batchRunning = false,
                    batchMsg = if (failures.isEmpty())
                        "${label}完成：$okCount/${accounts.size} 成功"
                    else "${label}完成：$okCount/${accounts.size} 成功，${failures.size} 失败"
                        + "\n" + failures.take(3).joinToString("\n"),
                )
            }
            loadState()
        }

    /** Daily check-in for every healthy account. */
    fun batchCheckin() = runBatch("一键签到") { acc ->
        val r = Api.post("/api/panel/points/checkin",
            JSONObject().put("name", acc.name), timeoutSec = 60)
        if (!r.optBoolean("ok", true)) {
            throw IllegalStateException(r.optString("error").ifBlank { "签到失败" })
        }
        if (r.optBoolean("already")) "已签到" else "签到成功"
    }

    /**
     * Onboarding tasks for every healthy account.  Each account runs eight
     * sequential upstream calls, so this can take minutes — the button stays
     * disabled and progress is shown per account.
     */
    fun batchOnboarding() = runBatch("一键新手任务") { acc ->
        val r = Api.post("/api/panel/points/onboarding",
            JSONObject().put("name", acc.name), timeoutSec = 300)
        if (!r.optBoolean("ok", true)) {
            throw IllegalStateException(r.optString("error").ifBlank { "任务失败" })
        }
        val tasks = r.optJSONArray("tasks") ?: JSONArray()
        val ok = (0 until tasks.length()).count { tasks.getJSONObject(it).optBoolean("ok") }
        "$ok/${tasks.length()}，+${r.optInt("gained")} 分"
    }

    /**
     * Bind an invitation code to every account that has none.
     *
     * One gateway call handles the whole pool: it picks spare codes from other
     * accounts, so no per-account loop is needed here. Unbound accounts show
     * up in [AccountView.appliedInviteCode] being blank.
     */
    fun batchInviteAuto() = viewModelScope.launch(crashGuard) {
        _ui.update { it.copy(batchRunning = true, batchMsg = "一键绑定邀请码：检查中…") }
        try {
            val r = Api.post("/api/panel/accounts/invite-auto", JSONObject(),
                timeoutSec = 600)
            if (!r.optBoolean("ok", true)) {
                throw IllegalStateException(r.optString("error").ifBlank { "绑定失败" })
            }
            val bound = r.optInt("bound", 0)
            val failed = r.optInt("failed", 0)
            val msg = if (bound == 0 && failed == 0) {
                r.optString("message").ifBlank { "所有账号均已绑定邀请码" }
            } else {
                "绑定成功 $bound" + (if (failed > 0) "，失败 $failed" else "")
            }
            _ui.update { it.copy(batchMsg = msg) }
            loadState(refresh = true)
        } catch (e: Exception) {
            _ui.update { it.copy(batchMsg = "一键绑定邀请码失败：${e.message}") }
        } finally {
            _ui.update { it.copy(batchRunning = false) }
        }
    }

    /**
     * Give one account a login password (empty = generate one upstream).
     *
     * Setting a password upgrades the account: its session can be renewed
     * automatically forever, so it no longer depends on an SMS code.
     */
    suspend fun setPassword(acc: AccountView, password: String): String = try {
        val body = JSONObject().put("name", acc.name)
        if (password.isNotBlank()) body.put("password", password)
        val r = Api.post("/api/panel/accounts/password", body, timeoutSec = 60)
        if (!r.optBoolean("ok", true)) {
            throw IllegalStateException(r.optString("error").ifBlank { "设置失败" })
        }
        loadState()
        "✓ 密码已设置：${r.optString("password")}" +
            (if (r.optBoolean("generated")) "（自动生成，请保存）" else "") +
            "\n账号已升级为永久，session 到期会自动续期。"
    } catch (e: Exception) {
        "失败：${e.message ?: e.javaClass.simpleName}"
    }

    /**
     * Upgrade every enabled account that has no password yet.
     *
     * Uses the batch endpoint: it skips accounts that already have a password
     * upstream (rotating an unknown password would break the user's own
     * login), keeps going when one account fails, and runs the upstream calls
     * concurrently.  [manual] applies the same password to every account;
     * empty means "generate one per account".
     */
    fun batchSetPasswords(manual: String = "") = viewModelScope.launch(crashGuard) {
        _ui.update { it.copy(batchRunning = true, batchMsg = "一键设置密码：提交中…") }
        try {
            val body = JSONObject()
            if (manual.isNotBlank()) body.put("password", manual)
            val r = Api.post("/api/panel/accounts/passwords", body, timeoutSec = 300)
            val results = r.optJSONArray("results") ?: JSONArray()
            val ok = mutableListOf<String>()
            val bad = mutableListOf<String>()
            var skipped = 0
            for (i in 0 until results.length()) {
                val row = results.getJSONObject(i)
                val name = row.optString("name")
                when {
                    row.optBoolean("ok") -> ok += "$name  ${row.optString("password")}"
                    row.optBoolean("skipped") -> skipped++
                    else -> bad += "$name: ${row.optString("error")}"
                }
            }
            loadState()
            _ui.update {
                it.copy(
                    batchRunning = false,
                    batchMsg = "一键设置密码：${ok.size}/${results.length()} 成功"
                        + (if (skipped > 0) "，$skipped 个已有密码跳过" else "")
                        + (if (bad.isNotEmpty()) "\n" + bad.take(3).joinToString("\n") else "")
                        + (if (ok.isNotEmpty())
                            "\n新密码：\n" + ok.take(10).joinToString("\n") else ""),
                )
            }
        } catch (e: Exception) {
            _ui.update {
                it.copy(batchRunning = false,
                    batchMsg = "一键设置密码失败：${e.message ?: e.javaClass.simpleName}")
            }
        }
    }

    /** Submit an invitation code for one account (one-shot, irreversible). */
    suspend fun applyInvite(acc: AccountView, code: String): String = try {
        val r = Api.post("/api/panel/points/activate",
            JSONObject().put("name", acc.name).put("code", code), timeoutSec = 90)
        val res = r.optJSONObject("result") ?: JSONObject()
        loadState()
        if (res.optBoolean("alreadyActivated")) {
            "该账号已激活过（已用 ${res.optString("appliedInvitationCode").ifBlank { "—" }}）"
        } else {
            "✓ 邀请码已提交，永久余额 ${res.opt("permanentBalance") ?: "—"}"
        }
    } catch (e: Exception) {
        "失败：${e.message ?: e.javaClass.simpleName}"
    }

    // -- fysms SMS registration ------------------------------------------

    /** Login + balance only — takes no number, spends no money. */
    suspend fun fysmsBalance(username: String, password: String): String = try {
        val body = JSONObject().put("fysms_username", username)
        if (password.isNotBlank()) body.put("fysms_password", password)
        val r = Api.post("/api/panel/fysms/balance", body, timeoutSec = 60)
        loadState()
        val balance = r.optDouble("balance", Double.NaN)
        if (balance.isNaN()) "余额解析失败"
        else "余额 ${"%.2f".format(balance)} 元 · 约可注册 " +
            "${r.optInt("affordable")} 个（${r.optDouble("price", 1.6)} 元/个）"
    } catch (e: Exception) {
        "失败：${e.message ?: e.javaClass.simpleName}"
    }

    /**
     * Register accounts through the fysms SMS platform.
     *
     * Spends real money (one code per number) and can run for minutes, so it
     * always sends ``confirm: true`` and keeps the button disabled until the
     * server answers.  Progress is reported per number in [UiState.batchMsg].
     */
    fun registerSms(username: String, password: String, count: Int,
                    keyword: String, accountPassword: String,
                    checkin: Boolean, onboarding: Boolean) =
        viewModelScope.launch(crashGuard) {
            _ui.update { it.copy(batchRunning = true,
                batchMsg = "接码注册：正在登录接码平台…") }
            try {
                val body = JSONObject()
                    .put("fysms_username", username)
                    .put("count", count)
                    .put("keyword", keyword)
                    .put("confirm", true)
                    .put("checkin", checkin)
                    .put("onboarding", onboarding)
                if (password.isNotBlank()) body.put("fysms_password", password)
                if (accountPassword.isNotBlank()) body.put("password", accountPassword)
                // count × (send + max_wait) can exceed any fixed timeout
                val r = Api.post("/api/panel/accounts/register-sms", body,
                    timeoutSec = 60L * 10L * count.coerceAtLeast(1))
                val results = r.optJSONArray("results") ?: JSONArray()
                val lines = mutableListOf<String>()
                for (i in 0 until results.length()) {
                    val row = results.getJSONObject(i)
                    val phone = row.optString("phone").ifBlank { "—" }
                    lines += if (row.optBoolean("ok")) {
                        val pwd = row.optString("password")
                        val tag = if (row.optBoolean("recycled")) "  ⚠回收号(无注册奖励)" else ""
                        "✓ $phone$tag" + (if (pwd.isNotBlank()) "  密码 $pwd" else "")
                    } else {
                        "✗ $phone  ${row.optString("error")}"
                    }
                }
                loadState()
                _ui.update {
                    it.copy(
                        batchRunning = false,
                        batchMsg = "接码注册完成：成功 ${r.optInt("done")}/" +
                            "${r.optInt("requested")}，余额 " +
                            "${r.optDouble("balance_before", Double.NaN)} → " +
                            "${r.optDouble("balance_after", Double.NaN)}" +
                            (if (r.has("charged") && !r.isNull("charged"))
                                "（扣费 ${"%.2f".format(r.optDouble("charged"))} 元）"
                            else "") + "\n" +
                            lines.joinToString("\n"),
                    )
                }
            } catch (e: Exception) {
                _ui.update {
                    it.copy(batchRunning = false,
                        batchMsg = "接码注册失败：${e.message ?: e.javaClass.simpleName}")
                }
            }
        }

    /** Read the stored login password of one account (for the show button). */
    suspend fun showPassword(acc: AccountView): String = try {
        val r = Api.post("/api/panel/accounts/password/show",
            JSONObject().put("name", acc.name), timeoutSec = 30)
        if (!r.optBoolean("ok", true)) {
            throw IllegalStateException(r.optString("error").ifBlank { "没有保存密码" })
        }
        "登录密码：${r.optString("password")}"
    } catch (e: Exception) {
        "失败：${e.message ?: e.javaClass.simpleName}"
    }

    fun refreshRecords() = viewModelScope.launch(crashGuard) {
        runCatching { loadRecords() }.onFailure { toast("读取失败：${it.message}") }
    }

    /** Tail the gateway's own log file (same source as the desktop panel). */
    fun refreshLog() = viewModelScope.launch(crashGuard) {
        runCatching {
            val r = Api.get("/api/panel/logs?lines=400")
            val arr = r.optJSONArray("lines") ?: JSONArray()
            val lines = (0 until arr.length()).map { arr.optString(it) }
            _ui.update { it.copy(logLines = lines, logPath = r.optString("path")) }
        }.onFailure { toast("读取日志失败：${it.message}") }
    }

    /**
     * Run a per-account action; returns the message to show in the dialog.
     *
     * The gateway answers HTTP 200 with `{"ok": false, "error": ...}` for
     * business failures (e.g. renewing an account that has no stored
     * password), so the body must be inspected, not just the status code.
     * `onboarding` runs eight sequential upstream calls and takes ~2 minutes,
     * hence its larger timeout.
     */
    suspend fun accountAction(kind: String, acc: AccountView): String = try {
        fun check(r: JSONObject) {
            if (!r.optBoolean("ok", true)) {
                throw IllegalStateException(r.optString("error").ifBlank { "操作失败" })
            }
        }
        when (kind) {
            "checkin" -> {
                val r = Api.post("/api/panel/points/checkin",
                    JSONObject().put("name", acc.name), timeoutSec = 60)
                check(r)
                if (r.optBoolean("already")) "今日已签到"
                else "签到成功，余额 ${r.opt("current_balance") ?: "—"}"
            }
            "onboarding" -> {
                val r = Api.post("/api/panel/points/onboarding",
                    JSONObject().put("name", acc.name), timeoutSec = 300)
                check(r)
                val tasks = r.optJSONArray("tasks") ?: JSONArray()
                val ok = (0 until tasks.length()).count { tasks.getJSONObject(it).optBoolean("ok") }
                "新手任务 $ok/${tasks.length()} 完成，到账 ${r.optInt("gained")} 积分"
            }
            "renew" -> {
                val r = Api.post("/api/panel/accounts/renew",
                    JSONObject().put("name", acc.name), timeoutSec = 60)
                check(r)
                loadState(); "已续期"
            }
            "quota" -> {
                // /accounts/update with only a name is an empty patch — it does
                // not refresh anything.  Force a real quota read instead.
                loadState(refresh = true)
                "已刷新"
            }
            "identity" -> {
                val r = Api.post("/api/panel/accounts/identity",
                    JSONObject().put("name", acc.name).put("regenerate", true))
                check(r)
                loadState()
                "已换新设备标识：${r.optJSONObject("identity")?.optString("devid") ?: "—"}"
            }
            "toggle" -> {
                val r = Api.post("/api/panel/accounts/update",
                    JSONObject().put("name", acc.name).put("enabled", !acc.enabled))
                check(r)
                loadState()
                if (acc.enabled) "已禁用" else "已启用"
            }
            "remove" -> {
                val r = Api.post("/api/panel/accounts/remove",
                    JSONObject().put("name", acc.name))
                check(r)
                loadState(); "已删除"
            }
            else -> "未知操作"
        }
    } catch (e: Exception) {
        val reason = when (e) {
            is java.net.SocketTimeoutException -> "请求超时（上游较慢，请稍后重试）"
            else -> e.message ?: e.javaClass.simpleName
        }
        "失败：$reason"
    }

    /**
     * Add (and optionally log in) an account.  Suspend, so a caller's
     * `finally { busy = false }` runs only after the request really finished —
     * launching internally made the dialog's spinner vanish immediately.
     */
    suspend fun addAccount(name: String, phone: String, password: String,
                           session: String, login: Boolean,
                           onDone: (String) -> Unit) {
        runCatching {
            val body = JSONObject().put("name", name)
            if (phone.isNotBlank()) body.put("loginid", phone)
            if (password.isNotBlank()) body.put("password", password)
            if (session.isNotBlank()) body.put("session", session)
            body.put("login", login)
            val r = Api.post("/api/panel/accounts", body, timeoutSec = 90)
            loadState()
            r
        }.onSuccess { r ->
            // the endpoint answers 200 even when the login attempt failed
            val loggedIn = r.optBoolean("logged_in", !login)
            val err = r.optString("error")
            val rolledBack = r.optBoolean("rolled_back", false)
            onDone(
                when {
                    err.isNotBlank() && rolledBack ->
                        "添加失败：$err（已撤销，可直接重试）"
                    err.isNotBlank() -> "添加失败：$err"
                    login && !loggedIn -> "账号已添加，但登录失败"
                    else -> "已添加 $name"
                }
            )
        }.onFailure { onDone("添加失败：${it.message}") }
    }

    // -- SMS login -------------------------------------------------------

    /** Step 1: ask the upstream to send a code; returns the msgid or null. */
    suspend fun smsSend(phone: String): Pair<String?, String> = try {
        val r = Api.post("/api/panel/accounts/sms",
            JSONObject().put("loginid", phone), timeoutSec = 60)
        val msgid = r.optString("msgid")
        if (msgid.isBlank()) null to "上游未返回 msgid"
        else msgid to "验证码已发送"
    } catch (e: Exception) {
        null to "发送失败：${e.message}"
    }

    /** Step 2: exchange phone + code + msgid for a session. */
    suspend fun smsLogin(name: String, phone: String, code: String, msgid: String,
                         onDone: (String) -> Unit) {
        runCatching {
            val r = Api.post("/api/panel/accounts/sms-login", JSONObject()
                .put("name", name.ifBlank { phone })
                .put("loginid", phone)
                .put("code", code)
                .put("msgid", msgid), timeoutSec = 120)
            if (!r.optBoolean("ok", true)) {
                throw IllegalStateException(r.optString("error").ifBlank { "登录失败" })
            }
            loadState()
            r
        }.onSuccess { onDone("登录成功") }
            .onFailure { onDone("登录失败：${it.message}") }
    }

    // -- import / export -------------------------------------------------

    /** Read an accounts.json picked by the user and POST it. */
    suspend fun importAccountsFromUri(uri: android.net.Uri, onDone: (String) -> Unit) {
        val ctx = AssetBridge.appContext
        if (ctx == null) { onDone("应用上下文不可用"); return }
        val text = withContext(kotlinx.coroutines.Dispatchers.IO) {
            runCatching {
                ctx.contentResolver.openInputStream(uri)?.use {
                    it.readBytes().toString(Charsets.UTF_8)
                } ?: throw IllegalStateException("无法读取文件")
            }.getOrNull()
        }
        if (text == null) {
            onDone("读取文件失败")
            return
        }
        importAccounts(text, onDone)
    }

    /**
     * Fetch the account export (contains plaintext sessions/passwords) and
     * hand it to the caller, which writes it wherever the user chooses.
     */
    suspend fun exportAccounts(): String? = runCatching {
        Api.get("/api/panel/export").toString(2)
    }.onFailure { toast("导出失败：${it.message}") }.getOrNull()

    suspend fun importAccounts(text: String, onDone: (String) -> Unit) {
        runCatching {
            val parsed = JSONObject(text)
            val result = Api.post("/api/panel/accounts/import",
                parsed.put("mode", "update"))
            loadState()
            result
        }.onSuccess { r ->
            onDone("导入完成：新增 ${r.optJSONArray("added")?.length() ?: 0}，"
                + "更新 ${r.optJSONArray("updated")?.length() ?: 0}")
        }.onFailure { onDone("导入失败：${it.message}") }
    }

    fun saveConfig(changes: Map<String, Any>, onDone: (String) -> Unit) =
        viewModelScope.launch(crashGuard) {
            // Setting api_keys enables the gateway's auth gate for the very
            // request that writes it — authenticate with the *new* key, or the
            // save itself is rejected with 401.
            val previousKey = Api.apiKey
            val newKeys = changes["api_keys"]
            val newKey = when (newKeys) {
                is List<*> -> newKeys.firstOrNull()?.toString().orEmpty()
                is String -> newKeys.split(",").firstOrNull()?.trim().orEmpty()
                else -> previousKey
            }
            if (newKey.isNotEmpty()) Api.apiKey = newKey

            runCatching {
                val body = JSONObject()
                changes.forEach { (k, v) ->
                    // JSONObject.put would call toString() on a List, turning
                    // an empty key list into the literal "[]" — which the
                    // gateway then treats as one (wrong) API key.
                    if (v is List<*>) body.put(k, JSONArray(v)) else body.put(k, v)
                }
                Api.post("/api/panel/config", body)
                loadState()
            }.onSuccess { onDone("配置已保存") }
                .onFailure {
                    // roll the in-memory key back if the change was rejected
                    Api.apiKey = previousKey
                    onDone("保存失败：${it.message}")
                }
        }

    // -- in-app update (GitHub releases) ---------------------------------

    /** Check the latest release; returns a human-readable status line. */
    suspend fun checkUpdate(): String {
        val ctx = AssetBridge.appContext ?: return "应用上下文不可用"
        val (release, message) = Updater.check(ctx)
        if (release != null) {
            pendingRelease = release
            return "$message（${release.tag}）"
        }
        return message
    }

    /** Download and open the installer for the release found by [checkUpdate]. */
    suspend fun installUpdate(onProgress: (Int) -> Unit): String {
        val ctx = AssetBridge.appContext ?: return "应用上下文不可用"
        val release = pendingRelease ?: return "请先检查更新"
        val (file, message) = Updater.download(ctx, release, onProgress)
        if (file == null) return message
        return Updater.install(ctx, file)
    }

    fun toast(msg: String) {
        _ui.update { it.copy(toast = msg) }
        viewModelScope.launch(crashGuard) {
            delay(2500)
            if (_ui.value.toast == msg) _ui.update { it.copy(toast = null) }
        }
    }

    fun clearToast() { _ui.update { it.copy(toast = null) } }
}

// ===================================================================== theme

private val Blue = Color(0xFF2563EB)
private val BlueSoft = Color(0xFFEFF4FF)
private val Green = Color(0xFF16A34A)
private val GreenSoft = Color(0xFFE9F9EF)
private val Red = Color(0xFFDC2626)
private val RedSoft = Color(0xFFFDECEC)
private val Amber = Color(0xFFD97706)
private val AmberSoft = Color(0xFFFEF6E7)
private val Bg = Color(0xFFF5F6F8)
private val CardBg = Color.White
private val Ink = Color(0xFF111827)
private val Muted = Color(0xFF6B7280)
private val Line = Color(0xFFE8EAEE)

@Composable
fun LoomyTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = lightColorScheme(
            primary = Blue,
            onPrimary = Color.White,
            background = Bg,
            surface = CardBg,
            onSurface = Ink,
            outline = Line,
        ),
        // No hard-coded colours here: a TextStyle colour overrides
        // LocalContentColor, which made the dark Snackbar render dark text on
        // its dark background ("black on black"). Screens set their own
        // colours explicitly where they need them.
        typography = Typography(
            titleLarge = androidx.compose.ui.text.TextStyle(
                fontSize = 22.sp, fontWeight = FontWeight.Bold),
            titleMedium = androidx.compose.ui.text.TextStyle(
                fontSize = 16.sp, fontWeight = FontWeight.SemiBold),
            bodyMedium = androidx.compose.ui.text.TextStyle(fontSize = 14.sp),
            bodySmall = androidx.compose.ui.text.TextStyle(fontSize = 12.sp),
        ),
        content = content,
    )
}

// ===================================================================== app

enum class Tab(val label: String, val icon: ImageVector) {
    Home("首页", Icons.Outlined.Home),
    Accounts("账号", Icons.Outlined.AccountCircle),
    Models("模型", Icons.Outlined.Diamond),
    Records("记录", Icons.Outlined.List),
    Settings("设置", Icons.Outlined.Settings),
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun GatewayApp(vm: GatewayViewModel) {
    val ui by vm.ui.collectAsState()
    var tab by remember { mutableStateOf(Tab.Home) }
    val snackbar = remember { SnackbarHostState() }

    LaunchedEffect(ui.toast) {
        ui.toast?.let {
            snackbar.showSnackbar(it)
            vm.clearToast()
        }
    }

    LoomyTheme {
        Scaffold(
            containerColor = Bg,
            snackbarHost = {
                SnackbarHost(snackbar) { data ->
                    Snackbar(
                        snackbarData = data,
                        containerColor = Ink,
                        contentColor = Color.White,
                        actionColor = Color(0xFF93C5FD),
                    )
                }
            },
            bottomBar = {
                NavigationBar(containerColor = CardBg, tonalElevation = 0.dp) {
                    Tab.entries.forEach { t ->
                        NavigationBarItem(
                            selected = tab == t,
                            onClick = { tab = t },
                            icon = { Icon(t.icon, t.label) },
                            label = { Text(t.label, fontSize = 11.sp) },
                            colors = NavigationBarItemDefaults.colors(
                                selectedIconColor = Blue,
                                selectedTextColor = Blue,
                                indicatorColor = BlueSoft,
                                unselectedIconColor = Muted,
                                unselectedTextColor = Muted,
                            ),
                        )
                    }
                }
            },
        ) { pad ->
            Box(Modifier.padding(pad).fillMaxSize()) {
                if (ui.loading && ui.state == null) {
                    LoadingScreen()
                } else {
                    when (tab) {
                        Tab.Home -> HomeScreen(vm, ui)
                        Tab.Accounts -> AccountsScreen(vm, ui)
                        Tab.Models -> ModelsScreen(vm, ui)
                        Tab.Records -> RecordsScreen(vm, ui)
                        Tab.Settings -> SettingsScreen(vm, ui)
                    }
                }
            }
        }
    }
}

@Composable
private fun LoadingScreen() {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            CircularProgressIndicator(color = Blue)
            Spacer(Modifier.height(14.dp))
            Text("正在启动网关…", color = Muted)
        }
    }
}

// ================================================================ home

@Composable
private fun HomeScreen(vm: GatewayViewModel, ui: UiState) {
    val s = ui.state
    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp, 18.dp, 16.dp, 24.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        item { BrandHeader() }

        item {
            SectionCard {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Box(
                        Modifier.size(9.dp).background(
                            if (ui.ready) Green else Muted,
                            RoundedCornerShape(50))
                    )
                    Spacer(Modifier.width(9.dp))
                    Text(
                        if (ui.ready) "网关运行中" else "网关已停止",
                        fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
                    )
                    Spacer(Modifier.weight(1f))
                    Text("端口 ${GatewayService.PORT}", color = Muted, fontSize = 13.sp)
                }
                Spacer(Modifier.height(14.dp))
                if (ui.ready) {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedButton(
                            onClick = { vm.refreshAll() },
                            modifier = Modifier.weight(1f).height(46.dp),
                            shape = RoundedCornerShape(12.dp),
                        ) { Text("刷新状态") }
                        OutlinedButton(
                            onClick = { vm.restartGateway() },
                            modifier = Modifier.weight(1f).height(46.dp),
                            shape = RoundedCornerShape(12.dp),
                        ) { Text("重启网关") }
                    }
                } else {
                    Button(
                        onClick = { vm.startGateway() },
                        modifier = Modifier.fillMaxWidth().height(46.dp),
                        shape = RoundedCornerShape(12.dp),
                    ) { Text("启动网关", fontSize = 15.sp) }
                }
                Spacer(Modifier.height(10.dp))
                KeepAliveHint()
            }
        }

        item { SectionTitle("概览") }
        item {
            val t = s?.totals
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                StatCard("可用账号", "${t?.usable ?: 0}/${t?.accounts ?: 0}", Modifier.weight(1f))
                StatCard("累计请求", "${t?.totalRequests ?: 0}", Modifier.weight(1f))
            }
            Spacer(Modifier.height(10.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                StatCard("剩余积分", "${t?.available ?: 0}", Modifier.weight(1f))
                StatCard("总 Tokens", formatTokens(t?.tokens ?: 0), Modifier.weight(1f))
            }
        }

        item { SectionTitle("客户端接入") }
        item {
            SectionCard {
                CopyRow("OpenAI Base URL", "http://127.0.0.1:${GatewayService.PORT}/v1")
                Divider(Modifier.padding(vertical = 10.dp), color = Line)
                CopyRow("Anthropic Base URL", "http://127.0.0.1:${GatewayService.PORT}")
                Divider(Modifier.padding(vertical = 10.dp), color = Line)
        if (s?.config?.host == "0.0.0.0") {
                    CopyRow("局域网地址", "http://${localIp() ?: "127.0.0.1"}:${GatewayService.PORT}")
                } else {
                    Text("局域网地址", color = Muted, fontSize = 13.sp)
                    Spacer(Modifier.height(4.dp))
                    Text("未开启（设置 → 网络与安全 → 允许局域网访问）",
                        color = Muted, fontSize = 13.sp)
                }
                val tunnel by vm.tunnel.collectAsState()
                tunnel.url?.let { url ->
                    Divider(Modifier.padding(vertical = 10.dp), color = Line)
                    CopyRow("公网地址", url)
                }
                Divider(Modifier.padding(vertical = 10.dp), color = Line)
                Text("API Key", color = Muted, fontSize = 13.sp)
                Spacer(Modifier.height(4.dp))
                Text(
                    if (s?.config?.authRequired == true) "已设置（需鉴权）" else "未设置（免鉴权）",
                    fontSize = 15.sp,
                )
            }
        }

        if (ui.error != null) {
            item {
                SectionCard(background = RedSoft) {
                    Text(ui.error, color = Red, fontSize = 13.sp)
                }
            }
        }
    }
}

/**
 * Tells the user whether the OS will let the gateway keep running in the
 * background.  Without the battery-optimisation exemption most ROMs freeze or
 * kill the process a few minutes after the screen turns off, which looks like
 * "the gateway randomly stops".
 */
@Composable
private fun KeepAliveHint() {
    val ctx = androidx.compose.ui.platform.LocalContext.current
    val pm = remember {
        ctx.getSystemService(android.content.Context.POWER_SERVICE)
                as? android.os.PowerManager
    }
    var exempt by remember { mutableStateOf(pm?.isIgnoringBatteryOptimizations(ctx.packageName) == true) }

    Row(verticalAlignment = Alignment.CenterVertically) {
        Icon(
            if (exempt) Icons.Filled.CheckCircle else Icons.Filled.Warning,
            null,
            tint = if (exempt) Green else Amber,
            modifier = Modifier.size(16.dp),
        )
        Spacer(Modifier.width(6.dp))
        Text(
            if (exempt) "已加入电池优化白名单，后台可长期运行"
            else "未加入电池白名单，息屏后可能被系统冻结",
            color = if (exempt) Muted else Amber,
            fontSize = 12.sp,
            modifier = Modifier.weight(1f),
        )
        if (!exempt) {
            TextButton(onClick = {
                runCatching {
                    ctx.startActivity(android.content.Intent(
                        android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        android.net.Uri.parse("package:${ctx.packageName}")))
                }
                exempt = pm?.isIgnoringBatteryOptimizations(ctx.packageName) == true
            }) { Text("去设置", fontSize = 12.sp) }
        }
    }
}

@Composable
private fun BrandHeader() {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(
            Modifier.size(44.dp).background(Blue, RoundedCornerShape(12.dp)),
            contentAlignment = Alignment.Center,
        ) { Icon(Icons.Filled.Bolt, null, tint = Color.White) }
        Spacer(Modifier.width(12.dp))
        Column {
            Text("Loomy2API", fontSize = 22.sp, fontWeight = FontWeight.Bold)
            Text("本地 API 网关 · 账号池调度", color = Muted, fontSize = 12.sp)
        }
    }
}

@Composable
private fun SectionTitle(text: String) {
    Text(text, fontSize = 16.sp, fontWeight = FontWeight.SemiBold, color = Ink)
}

@Composable
private fun StatCard(label: String, value: String, modifier: Modifier = Modifier) {
    Card(
        modifier = modifier,
        shape = RoundedCornerShape(14.dp),
        colors = CardDefaults.cardColors(containerColor = CardBg),
        elevation = CardDefaults.cardElevation(0.dp),
    ) {
        Column(Modifier.padding(14.dp)) {
            Text(value, fontSize = 21.sp, fontWeight = FontWeight.Bold)
            Text(label, color = Muted, fontSize = 12.sp)
        }
    }
}

@Composable
private fun SectionCard(
    modifier: Modifier = Modifier,
    background: Color = CardBg,
    content: @Composable ColumnScope.() -> Unit,
) {
    Card(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(14.dp),
        colors = CardDefaults.cardColors(containerColor = background),
        elevation = CardDefaults.cardElevation(0.dp),
    ) {
        Column(Modifier.padding(16.dp), content = content)
    }
}

@Composable
private fun CopyRow(label: String, value: String) {
    val ctx = androidx.compose.ui.platform.LocalContext.current
    Row(verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(label, color = Muted, fontSize = 13.sp)
            Spacer(Modifier.height(3.dp))
            Text(value, fontSize = 14.sp, fontFamily = FontFamily.Monospace,
                maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
        TextButton(onClick = {
            val cm = ctx.getSystemService(android.content.Context.CLIPBOARD_SERVICE)
                    as android.content.ClipboardManager
            cm.setPrimaryClip(android.content.ClipData.newPlainText(label, value))
            android.widget.Toast.makeText(ctx, "已复制", android.widget.Toast.LENGTH_SHORT).show()
        }) { Text("复制", color = Blue) }
    }
}

// ================================================================ accounts

@Composable
private fun AccountsScreen(vm: GatewayViewModel, ui: UiState) {
    var selected by remember { mutableStateOf<AccountView?>(null) }
    var showAdd by remember { mutableStateOf(false) }
    var showImport by remember { mutableStateOf(false) }
    var showBatchPwd by remember { mutableStateOf(false) }
    var showSmsReg by remember { mutableStateOf(false) }
    val ctx = androidx.compose.ui.platform.LocalContext.current
    val scope = rememberCoroutineScope()
    val accounts = ui.state?.accounts.orEmpty()

    // Export via SAF so the file lands where the user picks (Downloads etc.)
    // rather than in an app-private folder they cannot find.
    val exportLauncher = androidx.activity.compose.rememberLauncherForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.CreateDocument(
            "application/json")
    ) { uri ->
        if (uri != null) {
            scope.launch {
                val json = vm.exportAccounts() ?: return@launch
                runCatching {
                    withContext(Dispatchers.IO) {
                        ctx.contentResolver.openOutputStream(uri)?.use {
                            it.write(json.toByteArray(Charsets.UTF_8))
                        } ?: throw IllegalStateException("无法写入文件")
                    }
                }.onSuccess {
                    android.widget.Toast.makeText(ctx, "账号已导出（含明文 session，请妥善保管）",
                        android.widget.Toast.LENGTH_LONG).show()
                }.onFailure {
                    android.widget.Toast.makeText(ctx, "导出失败：${it.message}",
                        android.widget.Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    Box(Modifier.fillMaxSize()) {
        LazyColumn(
            contentPadding = PaddingValues(16.dp, 16.dp, 16.dp, 90.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("账号", fontSize = 24.sp, fontWeight = FontWeight.Bold)
                    Spacer(Modifier.weight(1f))
                    TextButton(onClick = { showImport = true }) { Text("导入") }
                    TextButton(onClick = {
                        exportLauncher.launch("loomy2api-accounts-${SimpleDateFormat(
                            "yyyyMMdd-HHmm", Locale.US).format(Date())}.json")
                    }) { Text("导出") }
                    TextButton(onClick = { vm.refreshAll() }) { Text("刷新") }
                }
                Text("共 ${accounts.size} 个账号，可用 ${accounts.count { it.healthy }} 个",
                    color = Muted, fontSize = 13.sp)
            }

            item {
                SectionCard {
                    Text("批量操作", color = Muted, fontSize = 13.sp)
                    Spacer(Modifier.height(10.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(
                            onClick = { vm.batchCheckin() },
                            enabled = !ui.batchRunning,
                            modifier = Modifier.weight(1f),
                            shape = RoundedCornerShape(10.dp),
                        ) { Text("一键签到") }
                        Button(
                            onClick = { vm.batchOnboarding() },
                            enabled = !ui.batchRunning,
                            modifier = Modifier.weight(1f),
                            shape = RoundedCornerShape(10.dp),
                        ) { Text("一键新手任务") }
                    }
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedButton(
                            onClick = { vm.refreshQuota() },
                            modifier = Modifier.weight(1f),
                            shape = RoundedCornerShape(10.dp),
                        ) { Text("刷新额度") }
                        OutlinedButton(
                            onClick = { vm.refreshModels() },
                            modifier = Modifier.weight(1f),
                            shape = RoundedCornerShape(10.dp),
                        ) { Text("刷新模型") }
                    }
                    Spacer(Modifier.height(8.dp))
                    OutlinedButton(
                        onClick = { vm.checkPool() },
                        enabled = !ui.batchRunning,
                        modifier = Modifier.fillMaxWidth(),
                        shape = RoundedCornerShape(10.dp),
                    ) { Text("检测续期（修复失效账号）") }
                    Spacer(Modifier.height(8.dp))
                    OutlinedButton(
                        onClick = { showBatchPwd = true },
                        enabled = !ui.batchRunning,
                        modifier = Modifier.fillMaxWidth(),
                        shape = RoundedCornerShape(10.dp),
                    ) { Text("一键设置密码（升级为永久账号）") }
                    OutlinedButton(
                        onClick = { vm.batchInviteAuto() },
                        enabled = !ui.batchRunning,
                        modifier = Modifier.fillMaxWidth(),
                        shape = RoundedCornerShape(10.dp),
                    ) { Text("一键绑定邀请码（未绑定的账号自动补齐）") }
                    // The lite build ships without SMS registration.
                    if (BuildConfig.ENABLE_SMS_REGISTRATION) {
                        Spacer(Modifier.height(8.dp))
                        OutlinedButton(
                            onClick = { showSmsReg = true },
                            enabled = !ui.batchRunning,
                            modifier = Modifier.fillMaxWidth(),
                            shape = RoundedCornerShape(10.dp),
                        ) { Text("接码注册（fysms · 收费）") }
                    }
                    if (ui.batchRunning || ui.batchMsg.isNotBlank()) {
                        Spacer(Modifier.height(10.dp))
                        if (ui.batchRunning) {
                            LinearProgressIndicator(Modifier.fillMaxWidth(), color = Blue)
                            Spacer(Modifier.height(6.dp))
                        }
                        Text(ui.batchMsg, color = if (ui.batchMsg.startsWith("失败")) Red else Muted,
                            fontSize = 12.sp)
                    }
                }
            }

            items(accounts, key = { it.name }) { acc ->
                AccountCard(acc, onClick = { selected = acc },
                    onRefreshQuota = { vm.refreshQuota() },
                    modelIds = ui.state?.totals?.modelIds.orEmpty())
            }
        }

        Button(
            onClick = { showAdd = true },
            modifier = Modifier.align(Alignment.BottomEnd)
                .padding(end = 18.dp, bottom = 18.dp)
                .height(52.dp),
            shape = RoundedCornerShape(16.dp),
        ) {
            Icon(Icons.Filled.Add, null)
            Spacer(Modifier.width(6.dp))
            Text("添加账号", fontSize = 15.sp)
        }
    }

    // Re-resolve the selection against the latest state: after an action
    // (disable, renew, …) the dialog must show the new values instead of the
    // snapshot taken when it was opened.
    selected?.let { chosen ->
        val fresh = accounts.firstOrNull { it.name == chosen.name } ?: chosen
        AccountDialog(vm, fresh, onDismiss = { selected = null })
    }
    if (showAdd) {
        AddAccountDialog(vm, onDismiss = { showAdd = false })
    }
    if (showImport) {
        ImportDialog(vm, onDismiss = { showImport = false })
    }
    if (showBatchPwd) {
        BatchPasswordDialog(
            count = accounts.count { it.enabled && !it.permanent },
            onDismiss = { showBatchPwd = false },
            onConfirm = { pwd ->
                showBatchPwd = false
                vm.batchSetPasswords(pwd)
            },
        )
    }
    if (showSmsReg) {
        SmsRegisterDialog(
            vm = vm,
            cfg = ui.state?.config,
            onDismiss = { showSmsReg = false },
            onConfirm = { user, pass, count, keyword, accountPwd, checkin, onboarding ->
                showSmsReg = false
                vm.registerSms(user, pass, count, keyword, accountPwd, checkin, onboarding)
            },
        )
    }
}

/**
 * fysms 接码注册 dialog: credentials, count, SMS keyword and the account
 * password to set.  Registration costs real money per number, so the confirm
 * button spells out the estimated charge and asks once more before starting.
 */
@Composable
private fun SmsRegisterDialog(
    vm: GatewayViewModel,
    cfg: ConfigView?,
    onDismiss: () -> Unit,
    onConfirm: (String, String, Int, String, String, Boolean, Boolean) -> Unit,
) {
    var user by remember { mutableStateOf(cfg?.fysmsUsername ?: "") }
    var pass by remember { mutableStateOf("") }
    var count by remember { mutableStateOf("1") }
    var keyword by remember { mutableStateOf(cfg?.fysmsKeyword ?: "讯飞") }
    var accountPwd by remember { mutableStateOf("") }
    var checkin by remember { mutableStateOf(false) }
    var onboarding by remember { mutableStateOf(false) }
    var balanceMsg by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var askCharge by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    // Second, explicit confirmation: taking a number costs money even when no
    // SMS ever arrives, so never start on a single tap.
    if (askCharge) {
        val n = (count.toIntOrNull() ?: 1).coerceIn(1, 20)
        AlertDialog(
            onDismissRequest = { askCharge = false },
            containerColor = CardBg,
            shape = RoundedCornerShape(18.dp),
            title = { Text("确认扣费", fontSize = 18.sp, fontWeight = FontWeight.Bold) },
            text = {
                Text(
                    "将取 $n 个号码（老版虚卡）。接码平台在成功取到验证码时扣费：" +
                        "1.6 元/个，首次 65%；没收到短信不扣费，最高约 " +
                        "%.2f 元。".format(n * 1.6),
                    fontSize = 13.sp,
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    askCharge = false
                    onConfirm(user.trim(), pass, n, keyword.trim(),
                        accountPwd.trim(), checkin, onboarding)
                }) { Text("确认开始") }
            },
            dismissButton = {
                TextButton(onClick = { askCharge = false }) { Text("取消") }
            },
        )
    }

    AlertDialog(
        onDismissRequest = { if (!busy) onDismiss() },
        containerColor = CardBg,
        shape = RoundedCornerShape(18.dp),
        title = { Text("接码注册（fysms）", fontSize = 19.sp, fontWeight = FontWeight.Bold) },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                OutlinedTextField(user, { user = it },
                    label = { Text("fysms 账号") },
                    singleLine = true, modifier = Modifier.fillMaxWidth())
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(pass, { pass = it },
                    label = { Text("fysms 密码") },
                    placeholder = {
                        Text(if (cfg?.fysmsPasswordSet == true)
                            "已保存，留空使用已保存密码" else "接码平台密码")
                    },
                    visualTransformation = PasswordVisualTransformation(),
                    singleLine = true, modifier = Modifier.fillMaxWidth())
                Spacer(Modifier.height(8.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(count, { count = it },
                        label = { Text("数量 (1-20)") },
                        singleLine = true,
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                        modifier = Modifier.weight(1f))
                    OutlinedTextField(keyword, { keyword = it },
                        label = { Text("短信标题") },
                        singleLine = true, modifier = Modifier.weight(1f))
                }
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(accountPwd, { accountPwd = it },
                    label = { Text("统一登录密码（留空=随机生成）") },
                    placeholder = { Text("至少 8 位，含字母和数字") },
                    singleLine = true, modifier = Modifier.fillMaxWidth())
                Spacer(Modifier.height(8.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(checkin, { checkin = it })
                    Text("注册后签到", fontSize = 13.sp)
                    Spacer(Modifier.width(8.dp))
                    Checkbox(onboarding, { onboarding = it })
                    Text("新手任务", fontSize = 13.sp)
                }
                Spacer(Modifier.height(8.dp))
                OutlinedButton(
                    onClick = {
                        if (busy) return@OutlinedButton
                        busy = true
                        balanceMsg = "查询中…"
                        scope.launch {
                            balanceMsg = vm.fysmsBalance(user.trim(), pass)
                            busy = false
                        }
                    },
                    enabled = !busy && user.isNotBlank(),
                    shape = RoundedCornerShape(10.dp),
                ) { Text("查询余额（免费）", fontSize = 13.sp) }
                if (balanceMsg.isNotBlank()) {
                    Spacer(Modifier.height(6.dp))
                    Text(balanceMsg, fontSize = 12.sp,
                        color = if (balanceMsg.startsWith("失败")) Red else Muted)
                }
                Spacer(Modifier.height(12.dp))
                Text(
                    "⚠ 接码平台在成功取到验证码时扣费（1.6 元/个，首次 65%）；" +
                        "没收到短信不扣费。平台需直连，请关闭 VPN / 全局代理。",
                    fontSize = 12.sp, color = Amber,
                )
            }
        },
        confirmButton = {
            TextButton(
                onClick = { askCharge = true },
                enabled = !busy && user.isNotBlank() &&
                    (pass.isNotBlank() || cfg?.fysmsPasswordSet == true),
            ) { Text("开始（收费）") }
        },
        dismissButton = { TextButton(onClick = onDismiss, enabled = !busy) { Text("取消") } },
    )
}

@Composable
private fun AccountCard(acc: AccountView, onClick: () -> Unit,
                        onRefreshQuota: () -> Unit,
                        modelIds: List<String> = emptyList()) {
    Card(
        modifier = Modifier.fillMaxWidth().clickable(onClick = onClick),
        shape = RoundedCornerShape(14.dp),
        colors = CardDefaults.cardColors(containerColor = CardBg),
        elevation = CardDefaults.cardElevation(0.dp),
    ) {
        Column(Modifier.padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    Modifier.size(9.dp).background(
                        if (acc.healthy) Green else if (acc.inCooldown) Amber else Red,
                        RoundedCornerShape(50))
                )
                Spacer(Modifier.width(9.dp))
                Text(acc.name, fontSize = 17.sp, fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.weight(1f))
                StatusChip(acc, modelIds)
            }
            Spacer(Modifier.height(10.dp))
            Text(
                "可用 ${acc.available ?: "—"} · 永久 ${acc.balance ?: "—"} · 活动 ${acc.dailyBalance ?: "—"}",
                color = Muted, fontSize = 13.sp,
            )
            Spacer(Modifier.height(4.dp))
            Text(
                "请求 ${acc.requests} · 失败 ${acc.failures}"
                    + if (acc.permanent) " · 永久（可自动续期）"
                      else (acc.sessionDaysLeft?.let { " · 剩余 ${"%.1f".format(it)} 天" } ?: "")
                    + when (acc.activated) {
                        true -> " · 已激活"
                        false -> " · 未激活"
                        null -> ""
                    },
                color = if (acc.permanent) Green else Muted, fontSize = 12.sp,
            )
            Spacer(Modifier.height(12.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(18.dp)) {
                ActionLink("管理") { onClick() }
                // direct action: the card link used to just open the dialog
                ActionLink("刷额度") { onRefreshQuota() }
            }
        }
    }
}

@Composable
private fun StatusChip(acc: AccountView, modelIds: List<String> = emptyList()) {
    // An entitlement downgrade outranks the balance/session states below: the
    // account answers fine, it just cannot serve most models any more, and the
    // upstream substitutes a free model instead of returning an error.
    val (bg, fg, label) = when {
        !acc.enabled -> Triple(Line, Muted, acc.statusLabel)
        acc.banned -> Triple(RedSoft, Red, acc.statusLabel)
        acc.inCooldown -> Triple(AmberSoft, Amber, acc.statusLabel)
        !acc.hasSession -> Triple(RedSoft, Red, acc.statusLabel)
        acc.hasNoModels -> Triple(RedSoft, Red, "无可用模型")
        acc.isDowngraded(modelIds) -> Triple(AmberSoft, Amber, "已降级")
        // a password account renews itself — an imminent expiry is not a risk
        acc.permanent -> Triple(GreenSoft, Green, acc.statusLabel)
        acc.sessionDaysLeft != null && acc.sessionDaysLeft < 3 ->
            Triple(AmberSoft, Amber, acc.statusLabel)
        else -> Triple(GreenSoft, Green, acc.statusLabel)
    }
    Box(
        Modifier.background(bg, RoundedCornerShape(20.dp)).padding(horizontal = 10.dp, vertical = 4.dp)
    ) { Text(label, color = fg, fontSize = 12.sp) }
}

@Composable
private fun ActionLink(text: String, onClick: () -> Unit) {
    Text(text, color = Blue, fontSize = 14.sp, modifier = Modifier.clickable(onClick = onClick))
}

// ================================================================ dialogs

/**
 * Confirmation for "一键设置密码": explains the effect and lets the user type
 * one password for every account (empty = generate one per account).
 */
@Composable
private fun BatchPasswordDialog(
    count: Int,
    onDismiss: () -> Unit,
    onConfirm: (String) -> Unit,
    title: String = "一键设置密码",
    intro: String = "",
) {
    var pwd by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = CardBg,
        shape = RoundedCornerShape(18.dp),
        title = { Text(title, fontSize = 19.sp, fontWeight = FontWeight.Bold) },
        text = {
            Column {
                Text(
                    if (intro.isNotBlank()) intro
                    else if (count == 0) "所有启用账号都已有密码，无需设置。"
                    else "将给 $count 个无密码账号设置登录密码。设置后它们升级为永久账号："
                        + "session 到期会自动用密码重登，不再需要短信验证码。",
                    fontSize = 13.sp, color = if (count == 0 && intro.isBlank()) Muted else Ink,
                )
                if (count > 0) {
                    Spacer(Modifier.height(8.dp))
                    Text("密码会保存到账号文件并随导出一起导出。",
                        fontSize = 12.sp, color = Muted)
                    Spacer(Modifier.height(12.dp))
                    OutlinedTextField(
                        pwd, { pwd = it },
                        label = { Text("统一密码（留空 = 自动生成）") },
                        placeholder = { Text("至少 8 位，含字母和数字") },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
            }
        },
        confirmButton = {
            TextButton(
                onClick = { onConfirm(pwd.trim()) },
                enabled = count > 0,
            ) { Text("开始设置") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

@Composable
private fun AccountDialog(vm: GatewayViewModel, acc: AccountView, onDismiss: () -> Unit) {
    var msg by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var showPwdDialog by remember { mutableStateOf(false) }
    var showInviteDialog by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    if (showInviteDialog) {
        InviteDialog(
            acc = acc,
            onDismiss = { showInviteDialog = false },
            onConfirm = { code ->
                showInviteDialog = false
                busy = true
                msg = "正在提交邀请码…"
                scope.launch {
                    try {
                        msg = vm.applyInvite(acc, code)
                    } finally {
                        busy = false
                    }
                }
            },
        )
    }

    if (showPwdDialog) {
        BatchPasswordDialog(
            count = 1,
            title = if (acc.permanent) "重设密码" else "设置密码",
            intro = "为 ${acc.name} 设置登录密码。设置后账号升级为永久："
                + "session 到期会自动用密码重登。",
            onDismiss = { showPwdDialog = false },
            onConfirm = { pwd ->
                showPwdDialog = false
                busy = true
                msg = "正在设置密码…"
                scope.launch {
                    try {
                        msg = vm.setPassword(acc, pwd)
                    } finally {
                        busy = false
                    }
                }
            },
        )
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = CardBg,
        shape = RoundedCornerShape(18.dp),
        title = {
            Column {
                Text(acc.name, fontSize = 19.sp, fontWeight = FontWeight.Bold)
                Text(acc.loginidMasked, color = Muted, fontSize = 13.sp)
            }
        },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    MiniStat("状态", acc.statusLabel, Modifier.weight(1f))
                    MiniStat("可用积分", "${acc.available ?: "—"}", Modifier.weight(1f))
                }
                Spacer(Modifier.height(8.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    MiniStat("session 剩余",
                        if (acc.permanent) "永久"
                        else acc.sessionDaysLeft?.let { "%.1f 天".format(it) } ?: "—",
                        Modifier.weight(1f))
                    MiniStat("设备", acc.devid.ifBlank { "—" }, Modifier.weight(1f))
                }
                Spacer(Modifier.height(8.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    MiniStat("永久余额", "${acc.balance ?: "—"}", Modifier.weight(1f))
                    MiniStat("活动余额", "${acc.dailyBalance ?: "—"}", Modifier.weight(1f))
                }
                Spacer(Modifier.height(8.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    MiniStat("激活状态",
                        when (acc.activated) {
                            true -> "已激活"
                            false -> "未激活"
                            null -> "未知"
                        }, Modifier.weight(1f))
                    MiniStat("已绑定邀请码",
                        acc.appliedInvitationCode.ifBlank { "未绑定" }, Modifier.weight(1f))
                }
                Spacer(Modifier.height(8.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    MiniStat("号龄 / 入池",
                        (if (acc.recycled == true) "回收号 " else "")
                        + fmtShortTime(acc.obtainedAt) + " · "
                        + (acc.ledgerTotal?.let { "$it 条" } ?: "?"),
                        Modifier.weight(1f))
                }
                // The upstream scopes models per account and silently swaps in
                // a free one for anything the account lacks, so this list is
                // the only way to tell what an account can really serve.
                Spacer(Modifier.height(8.dp))
                val uiState by vm.ui.collectAsState()
                val allIds = uiState.state?.totals?.modelIds.orEmpty()
                Column {
                    Text(
                        when {
                            acc.models == null -> "可用模型：未探测"
                            acc.hasNoModels -> "可用模型：0 个（不参与路由）"
                            acc.isDowngraded(allIds) ->
                                "可用模型：${acc.models!!.size} 个 · 已降级"
                            else -> "可用模型：${acc.models!!.size} 个"
                        },
                        color = if (acc.hasNoModels || acc.isDowngraded(allIds)) Amber else Muted,
                        fontSize = 12.sp, fontWeight = FontWeight.Medium,
                    )
                    acc.models?.forEach { id ->
                        Text("· $id", color = Muted, fontSize = 11.sp)
                    }
                    if (acc.isDowngraded(allIds)) {
                        Text("请求其它模型会被上游静默换成免费模型，路由会避开该账号",
                            color = Amber, fontSize = 11.sp)
                    }
                }
                Spacer(Modifier.height(14.dp))

                if (acc.banned) {
                    Text(
                        "该账号已被上游停用（封禁），已停止参与路由。",
                        color = Red, fontSize = 13.sp,
                        modifier = Modifier.padding(bottom = 10.dp),
                    )
                }

                // password state: an account without one cannot renew itself
                Row(
                    modifier = Modifier.fillMaxWidth().padding(bottom = 10.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(Modifier.weight(1f)) {
                        Text(
                            when {
                                acc.permanent -> "永久账号（密码已验证）"
                                acc.passwordSetAt > 0 -> "密码未验证：请重设以确认可用"
                                else -> "未设密码：到期后需短信重新登录"
                            },
                            fontSize = 13.sp,
                            color = if (acc.permanent) Green else Amber,
                        )
                        if (!acc.permanent) {
                            Text(
                                if (acc.passwordSetAt > 0)
                                    "上次设置成功但登录验证未通过，到期可能无法自动续期"
                                else "设置密码后 session 到期会自动续期",
                                fontSize = 11.sp, color = Muted)
                        }
                    }
                    OutlinedButton(
                        onClick = { showPwdDialog = true },
                        enabled = !busy && acc.hasSession,
                        shape = RoundedCornerShape(10.dp),
                    ) { Text(if (acc.permanent) "重设密码" else "设置密码", fontSize = 13.sp) }
                }
                if (acc.permanent) {
                    Row(
                        modifier = Modifier.fillMaxWidth().padding(bottom = 10.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        OutlinedButton(
                            onClick = {
                                if (busy) return@OutlinedButton
                                busy = true
                                msg = "正在读取密码…"
                                scope.launch {
                                    try {
                                        val text = vm.showPassword(acc)
                                        msg = text
                                        if (!text.startsWith("失败")) {
                                            val pwd = text.substringAfter("登录密码：")
                                            copyToClipboard(pwd)
                                            msg = "$text\n（已复制到剪贴板）"
                                        }
                                    } finally {
                                        busy = false
                                    }
                                }
                            },
                            enabled = !busy,
                            modifier = Modifier.fillMaxWidth(),
                            shape = RoundedCornerShape(10.dp),
                        ) { Text("显示已保存密码（复制）", fontSize = 13.sp) }
                    }
                }

                val actions = listOf(
                    "查积分" to "quota", "每日签到" to "checkin",
                    "新手任务" to "onboarding", "续期重登" to "renew",
                    "填写邀请码" to "invite", "换设备标识" to "identity",
                    if (acc.enabled) "禁用" to "toggle" else "启用" to "toggle",
                )
                actions.chunked(2).forEach { pair ->
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp),
                        modifier = Modifier.fillMaxWidth().padding(bottom = 8.dp)) {
                        pair.forEach { (label, kind) ->
                            OutlinedButton(
                                onClick = {
                                    if (busy) return@OutlinedButton
                                    if (kind == "invite") {
                                        showInviteDialog = true
                                        return@OutlinedButton
                                    }
                                    busy = true
                                    msg = "正在执行：$label…"
                                    scope.launch {
                                        try {
                                            msg = vm.accountAction(kind, acc)
                                        } finally {
                                            busy = false   // never leave it spinning
                                        }
                                    }
                                },
                                enabled = !busy,
                                modifier = Modifier.weight(1f),
                                shape = RoundedCornerShape(10.dp),
                            ) { Text(label, fontSize = 13.sp) }
                        }
                        if (pair.size == 1) Spacer(Modifier.weight(1f))
                    }
                }
                OutlinedButton(
                    onClick = {
                        if (busy) return@OutlinedButton
                        busy = true
                        msg = "正在删除…"
                        scope.launch {
                            val result = try {
                                vm.accountAction("remove", acc)
                            } finally {
                                busy = false
                            }
                            msg = result
                            if (!result.startsWith("失败")) onDismiss()
                        }
                    },
                    enabled = !busy,
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(10.dp),
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = Red),
                ) { Text("删除账号", color = Red) }

                if (busy) {
                    Spacer(Modifier.height(12.dp))
                    LinearProgressIndicator(Modifier.fillMaxWidth(), color = Blue)
                }
                if (msg.isNotBlank()) {
                    Spacer(Modifier.height(12.dp))
                    Text(msg, fontSize = 13.sp,
                        color = when {
                            msg.startsWith("失败") -> Red
                            msg.startsWith("正在") -> Muted
                            else -> Green
                        })
                }
            }
        },
        confirmButton = {
            TextButton(onClick = onDismiss) { Text("关闭") }
        },
    )
}

/** Enter an invitation code for one account. Irreversible, so confirm first. */
@Composable
private fun InviteDialog(
    acc: AccountView,
    onDismiss: () -> Unit,
    onConfirm: (String) -> Unit,
) {
    var code by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = CardBg,
        shape = RoundedCornerShape(18.dp),
        title = { Text("填写邀请码", fontSize = 19.sp, fontWeight = FontWeight.Bold) },
        text = {
            Column {
                Text("账号 ${acc.name}", fontSize = 13.sp, color = Muted)
                if (acc.appliedInvitationCode.isNotBlank()) {
                    Spacer(Modifier.height(6.dp))
                    Text("当前已绑定 ${acc.appliedInvitationCode}",
                        fontSize = 12.sp, color = Amber)
                }
                Spacer(Modifier.height(10.dp))
                OutlinedTextField(
                    code, { code = it },
                    label = { Text("邀请码") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                Text("每个账号只能填写一次，提交后不可撤销。",
                    fontSize = 12.sp, color = Muted)
            }
        },
        confirmButton = {
            TextButton(
                onClick = { onConfirm(code.trim()) },
                enabled = code.isNotBlank(),
            ) { Text("提交") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

@Composable
private fun MiniStat(label: String, value: String, modifier: Modifier = Modifier) {
    Column(
        modifier.background(Bg, RoundedCornerShape(10.dp)).padding(10.dp)
    ) {
        Text(label, color = Muted, fontSize = 11.sp)
        Spacer(Modifier.height(2.dp))
        Text(value, fontSize = 14.sp, fontWeight = FontWeight.SemiBold, maxLines = 1,
            overflow = TextOverflow.Ellipsis)
    }
}

/** "MM-dd HH:mm" for a unix-seconds timestamp (0/blank → "—"). */
private fun fmtShortTime(ts: Long): String {
    if (ts <= 0) return "—"
    return SimpleDateFormat("MM-dd HH:mm", Locale.US).format(Date(ts * 1000))
}

@Composable
private fun AddAccountDialog(vm: GatewayViewModel, onDismiss: () -> Unit) {
    var mode by remember { mutableStateOf("password") }   // password | sms | session
    var name by remember { mutableStateOf("") }
    var phone by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var session by remember { mutableStateOf("") }
    var login by remember { mutableStateOf(true) }
    var code by remember { mutableStateOf("") }
    var msgid by remember { mutableStateOf("") }
    var msg by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    AlertDialog(
        onDismissRequest = { if (!busy) onDismiss() },
        containerColor = CardBg,
        shape = RoundedCornerShape(18.dp),
        title = { Text("添加账号", fontWeight = FontWeight.Bold) },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    listOf("password" to "密码登录", "sms" to "验证码", "session" to "session")
                        .forEach { (key, label) ->
                            ChipButton(mode == key, label, {
                                mode = key; msg = ""
                            })
                        }
                }
                Spacer(Modifier.height(12.dp))

                OutlinedTextField(name, { name = it }, label = { Text("名称") },
                    singleLine = true, modifier = Modifier.fillMaxWidth())
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(phone, { phone = it }, label = { Text("手机号") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone),
                    modifier = Modifier.fillMaxWidth())

                when (mode) {
                    "password" -> {
                        Spacer(Modifier.height(8.dp))
                        OutlinedTextField(password, { password = it },
                            label = { Text("密码") }, singleLine = true,
                            visualTransformation = PasswordVisualTransformation(),
                            modifier = Modifier.fillMaxWidth())
                        Spacer(Modifier.height(6.dp))
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Checkbox(login, { login = it })
                            Text("添加后立即登录", fontSize = 13.sp)
                        }
                    }
                    "sms" -> {
                        Spacer(Modifier.height(8.dp))
                        Row(verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            OutlinedTextField(code, { code = it },
                                label = { Text("验证码") }, singleLine = true,
                                keyboardOptions = KeyboardOptions(
                                    keyboardType = KeyboardType.Number),
                                modifier = Modifier.weight(1f))
                            OutlinedButton(
                                onClick = {
                                    if (phone.isBlank()) { msg = "请先填手机号"; return@OutlinedButton }
                                    busy = true
                                    scope.launch {
                                        try {
                                            val (id, text) = vm.smsSend(phone)
                                            msgid = id ?: ""
                                            // clear the previous failure so a
                                            // stale "msgid required" message
                                            // does not sit under a fresh code
                                            msg = text
                                        } finally { busy = false }
                                    }
                                },
                                enabled = !busy,
                                shape = RoundedCornerShape(10.dp),
                            ) { Text(if (msgid.isBlank()) "获取验证码" else "重新获取") }
                        }
                        Spacer(Modifier.height(6.dp))
                        Text("验证码登录不需要密码，适合未设置密码的账号",
                            color = Muted, fontSize = 11.sp)
                    }
                    "session" -> {
                        Spacer(Modifier.height(8.dp))
                        OutlinedTextField(session, { session = it },
                            label = { Text("session") }, singleLine = true,
                            modifier = Modifier.fillMaxWidth())
                    }
                }

                if (busy) {
                    Spacer(Modifier.height(10.dp))
                    LinearProgressIndicator(Modifier.fillMaxWidth(), color = Blue)
                }
                if (msg.isNotBlank()) {
                    Spacer(Modifier.height(8.dp))
                    Text(msg, fontSize = 13.sp,
                        color = if (msg.contains("失败")) Red else Green)
                }
            }
        },
        confirmButton = {
            TextButton(
                enabled = !busy,
                onClick = {
                    when {
                        name.isBlank() && mode != "sms" -> msg = "请填写名称"
                        mode == "sms" -> {
                            when {
                                phone.isBlank() -> { msg = "请填写手机号"; return@TextButton }
                                msgid.isBlank() -> {
                                    msg = "请先点「获取验证码」"
                                    return@TextButton
                                }
                                code.isBlank() -> { msg = "请填写收到的验证码"; return@TextButton }
                            }
                            busy = true
                            scope.launch {
                                try {
                                    vm.smsLogin(name, phone, code, msgid) { msg = it }
                                } finally { busy = false }
                            }
                        }
                        mode == "session" -> {
                            if (session.isBlank()) { msg = "请填写 session"; return@TextButton }
                            busy = true
                            scope.launch {
                                try {
                                    vm.addAccount(name.ifBlank { phone }, phone, "", session,
                                        false) { msg = it }
                                } finally { busy = false }
                            }
                        }
                        else -> {
                            busy = true
                            scope.launch {
                                try {
                                    vm.addAccount(name, phone, password, "", login) { msg = it }
                                } finally { busy = false }
                            }
                        }
                    }
                },
            ) { Text(if (mode == "sms") "登录" else "添加") }
        },
        dismissButton = {
            TextButton(onClick = onDismiss, enabled = !busy) { Text("取消") }
        },
    )
}

@Composable
private fun ImportDialog(vm: GatewayViewModel, onDismiss: () -> Unit) {
    var text by remember { mutableStateOf("") }
    var msg by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val ctx = androidx.compose.ui.platform.LocalContext.current

    // SAF picker: lets the user pick an accounts.json from Downloads/Drive/etc.
    val picker = androidx.activity.compose.rememberLauncherForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.OpenDocument()
    ) { uri ->
        if (uri != null) {
            busy = true
            scope.launch {
                try {
                    vm.importAccountsFromUri(uri) { result -> msg = result }
                } finally {
                    busy = false
                }
            }
        }
    }

    AlertDialog(
        onDismissRequest = { if (!busy) onDismiss() },
        containerColor = CardBg,
        shape = RoundedCornerShape(18.dp),
        title = { Text("导入账号", fontWeight = FontWeight.Bold) },
        text = {
            Column {
                OutlinedButton(
                    onClick = {
                        picker.launch(arrayOf("application/json", "text/plain", "*/*"))
                    },
                    enabled = !busy,
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(10.dp),
                ) {
                    Icon(Icons.Filled.FolderOpen, null)
                    Spacer(Modifier.width(6.dp))
                    Text("选择 accounts.json 文件")
                }
                Spacer(Modifier.height(12.dp))
                Text("或直接粘贴内容（含 accounts 数组）", color = Muted, fontSize = 12.sp)
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(text, { text = it }, minLines = 5,
                    modifier = Modifier.fillMaxWidth().heightIn(max = 200.dp))
                if (busy) {
                    Spacer(Modifier.height(10.dp))
                    LinearProgressIndicator(Modifier.fillMaxWidth(), color = Blue)
                }
                if (msg.isNotBlank()) {
                    Spacer(Modifier.height(8.dp))
                    Text(msg, fontSize = 13.sp,
                        color = if (msg.contains("失败")) Red else Green)
                }
            }
        },
        confirmButton = {
            TextButton(
                enabled = !busy && text.isNotBlank(),
                onClick = {
                    busy = true
                    scope.launch {
                        try {
                            vm.importAccounts(text) { msg = it }
                        } finally { busy = false }
                    }
                },
            ) { Text("粘贴导入") }
        },
        dismissButton = {
            TextButton(onClick = onDismiss, enabled = !busy) { Text("关闭") }
        },
    )
}

// ================================================================ models

@Composable
private fun ModelsScreen(vm: GatewayViewModel, ui: UiState) {
    var query by remember { mutableStateOf("") }
    val models = ui.models.filter {
        query.isBlank() || it.id.contains(query, true) || it.name.contains(query, true)
    }
    LazyColumn(
        contentPadding = PaddingValues(16.dp, 16.dp, 16.dp, 24.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column {
                    Text("模型", fontSize = 24.sp, fontWeight = FontWeight.Bold)
                    Text("共 ${ui.models.size} 个 · 内置清单", color = Muted, fontSize = 13.sp)
                }
                Spacer(Modifier.weight(1f))
                OutlinedButton(onClick = { vm.refreshModels() },
                    shape = RoundedCornerShape(12.dp)) { Text("刷新") }
            }
        }
        item {
            OutlinedTextField(query, { query = it }, placeholder = { Text("搜索模型 id / 名称") },
                singleLine = true, modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(12.dp))
        }
        item {
            ui.state?.config?.defaultModel?.let { def ->
                Box(
                    Modifier.background(BlueSoft, RoundedCornerShape(12.dp))
                        .padding(horizontal = 14.dp, vertical = 10.dp)
                ) {
                    Row {
                        Text("默认模型", color = Blue, fontSize = 13.sp)
                        Spacer(Modifier.width(10.dp))
                        Text(def, color = Blue, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                    }
                }
            }
        }
        items(models, key = { it.id }) { m -> ModelCard(m) }
    }
}

@Composable
private fun ModelCard(m: ModelView) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(14.dp),
        colors = CardDefaults.cardColors(
            containerColor = if (m.isDefault) BlueSoft else CardBg),
        elevation = CardDefaults.cardElevation(0.dp),
    ) {
        Column(Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(m.name.ifBlank { m.id }, fontSize = 16.sp,
                    fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                if (m.isDefault) Text("默认", color = Blue, fontSize = 12.sp)
            }
            Spacer(Modifier.height(4.dp))
            Text(m.id, color = Muted, fontSize = 12.sp, fontFamily = FontFamily.Monospace)
            Spacer(Modifier.height(10.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                Chip("x${m.multiplier ?: "—"}")
                m.contextLength?.let { Chip("上下文 ${formatTokens(it)}") }
                Chip(capabilityText(m))
            }
        }
    }
}

@Composable
private fun Chip(text: String) {
    Box(Modifier.background(Bg, RoundedCornerShape(8.dp))
        .padding(horizontal = 9.dp, vertical = 5.dp)) {
        Text(text, color = Muted, fontSize = 11.sp)
    }
}

private fun capabilityText(m: ModelView): String {
    val caps = mutableListOf<String>()
    if (m.vision) caps += "image"
    if (m.reasoning) caps += "think"
    if (m.tools) caps += "tools"
    return if (caps.isEmpty()) "text" else "text/" + caps.joinToString("/")
}

// ================================================================ records

@Composable
private fun RecordsScreen(vm: GatewayViewModel, ui: UiState) {
    var filter by remember { mutableStateOf("all") }
    var pane by remember { mutableStateOf("calls") }   // calls | log
    val rows = when (filter) {
        "ok" -> ui.records.filter { it.ok }
        "fail" -> ui.records.filter { !it.ok }
        else -> ui.records
    }

    if (pane == "log") {
        RunLogPane(vm, ui, onSwitch = { pane = "calls" })
        return
    }
    LazyColumn(
        contentPadding = PaddingValues(16.dp, 16.dp, 16.dp, 24.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column {
                    Text("记录", fontSize = 24.sp, fontWeight = FontWeight.Bold)
                    Text("调用与请求日志", color = Muted, fontSize = 13.sp)
                }
                Spacer(Modifier.weight(1f))
                TextButton(onClick = { vm.refreshRecords() }) { Text("刷新") }
            }
        }
        item {
            // segmented control: call records vs raw gateway log
            Row(Modifier.fillMaxWidth()) {
                SegmentedButton("调用记录", pane == "calls",
                    Modifier.weight(1f)) { pane = "calls" }
                SegmentedButton("运行日志", pane == "log",
                    Modifier.weight(1f)) { pane = "log" }
            }
        }
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                ChipButton(filter == "all", "全部", { filter = "all" })
                ChipButton(filter == "ok", "成功", { filter = "ok" })
                ChipButton(filter == "fail", "失败", { filter = "fail" })
            }
        }
        if (rows.isEmpty()) {
            item {
                SectionCard {
                    Text(
                        if (ui.records.isEmpty()) "暂无记录。发起一次 API 请求后会显示在这里。"
                        else "没有符合条件的记录。",
                        color = Muted, fontSize = 13.sp,
                    )
                }
            }
        }
        // include the index: two calls in the same second to the same model
        // would otherwise collide and crash the lazy list
        itemsIndexed(rows) { i, r ->
            RecordCard(r)
        }
    }
}

@Composable
private fun SegmentedButton(text: String, selected: Boolean, modifier: Modifier = Modifier,
                            onClick: () -> Unit) {
    Box(
        modifier
            .padding(2.dp)
            .background(if (selected) CardBg else Color.Transparent, RoundedCornerShape(20.dp))
            .clickable(onClick = onClick)
            .padding(vertical = 9.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(text, fontSize = 14.sp,
            color = if (selected) Blue else Muted,
            fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal)
    }
}

@Composable
private fun RunLogPane(vm: GatewayViewModel, ui: UiState, onSwitch: () -> Unit) {
    val listState = androidx.compose.foundation.lazy.rememberLazyListState()

    LaunchedEffect(Unit) { vm.refreshLog() }
    // keep tailing while this pane is open
    LaunchedEffect(Unit) {
        while (true) {
            delay(5000)
            vm.refreshLog()
        }
    }
    // jump to the newest line whenever the log grows
    LaunchedEffect(ui.logLines.size) {
        if (ui.logLines.isNotEmpty()) {
            listState.scrollToItem(ui.logLines.lastIndex)
        }
    }

    Column(Modifier.fillMaxSize().padding(16.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column {
                Text("记录", fontSize = 24.sp, fontWeight = FontWeight.Bold)
                Text("调用与请求日志", color = Muted, fontSize = 13.sp)
            }
            Spacer(Modifier.weight(1f))
            TextButton(onClick = { vm.refreshLog() }) { Text("刷新") }
        }
        Spacer(Modifier.height(10.dp))
        Row(Modifier.fillMaxWidth()) {
            SegmentedButton("调用记录", false, Modifier.weight(1f)) { onSwitch() }
            SegmentedButton("运行日志", true, Modifier.weight(1f)) {}
        }
        Spacer(Modifier.height(10.dp))
        Text(ui.logPath, color = Muted, fontSize = 10.sp, fontFamily = FontFamily.Monospace,
            maxLines = 1, overflow = TextOverflow.Ellipsis)
        Spacer(Modifier.height(8.dp))
        Card(
            Modifier.fillMaxSize(),
            shape = RoundedCornerShape(14.dp),
            colors = CardDefaults.cardColors(containerColor = Color(0xFF0B0D11)),
        ) {
            if (ui.logLines.isEmpty()) {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Text("暂无日志", color = Muted, fontSize = 13.sp)
                }
            } else {
                LazyColumn(
                    state = listState,
                    contentPadding = PaddingValues(12.dp),
                    verticalArrangement = Arrangement.spacedBy(2.dp),
                ) {
                    itemsIndexed(ui.logLines) { _, line ->
                        Text(
                            line,
                            color = when {
                                line.contains("[error]") || line.contains("失败") -> Color(0xFFFF8A8A)
                                line.contains("[warn]") -> Color(0xFFF5C26B)
                                line.contains("http=200") -> Color(0xFF8FD9A8)
                                else -> Color(0xFFB9C2D4)
                            },
                            fontSize = 11.sp,
                            fontFamily = FontFamily.Monospace,
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun ChipButton(selected: Boolean, text: String, onClick: () -> Unit) {
    Box(
        Modifier
            .background(if (selected) Blue else CardBg, RoundedCornerShape(20.dp))
            .clickable(onClick = onClick)
            .padding(horizontal = 16.dp, vertical = 8.dp)
    ) {
        Text(text, color = if (selected) Color.White else Muted, fontSize = 13.sp)
    }
}

@Composable
private fun RecordCard(r: CallRecord) {
    val (bg, fg) = if (r.ok) GreenSoft to Green else RedSoft to Red
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(14.dp),
        colors = CardDefaults.cardColors(containerColor = CardBg),
        elevation = CardDefaults.cardElevation(0.dp),
    ) {
        Column(Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.background(bg, RoundedCornerShape(8.dp))
                    .padding(horizontal = 8.dp, vertical = 4.dp)) {
                    Text(if (r.ok) "成功" else "失败", color = fg, fontSize = 12.sp)
                }
                Spacer(Modifier.width(10.dp))
                Text(r.model, fontSize = 15.sp, fontWeight = FontWeight.SemiBold,
                    modifier = Modifier.weight(1f), maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(formatTime(r.ts), color = Muted, fontSize = 11.sp)
            }
            Spacer(Modifier.height(8.dp))
            Text(
                "${r.kind}${if (r.stream) " · stream" else ""} · ${"%.1f".format(r.seconds)}s"
                    + " · ${r.tokens} tok · ${r.points} 分",
                color = Muted, fontSize = 12.sp,
            )
            Spacer(Modifier.height(3.dp))
            Text("账号：${r.account}", color = Muted, fontSize = 12.sp)
            if (r.error.isNotBlank()) {
                Spacer(Modifier.height(6.dp))
                Text(r.error, color = Red, fontSize = 11.sp, maxLines = 3,
                    overflow = TextOverflow.Ellipsis)
            }
        }
    }
}

// ================================================================ settings

@Composable
private fun SettingsScreen(vm: GatewayViewModel, ui: UiState) {
    val cfg = ui.state?.config
    var strategy by remember(cfg) { mutableStateOf(cfg?.strategy ?: "balance") }
    var proxy by remember(cfg) { mutableStateOf(cfg?.proxy ?: "") }
    var apiKeys by remember(cfg) { mutableStateOf(cfg?.apiKeys?.joinToString(",") ?: "") }
    var logRequests by remember(cfg) { mutableStateOf(cfg?.logRequests ?: true) }
    var timeout by remember(cfg) { mutableStateOf("${cfg?.timeout ?: 1200}") }
    var cooldown by remember(cfg) { mutableStateOf("${cfg?.cooldownSeconds ?: 300}") }
    var renewDays by remember(cfg) {
        mutableStateOf("${cfg?.sessionRenewBeforeDays ?: 3}")
    }
    var pwdDays by remember(cfg) {
        mutableStateOf("${cfg?.passwordSetBeforeDays ?: 1}")
    }
    var autoSetPwd by remember(cfg) { mutableStateOf(cfg?.autoSetPassword ?: false) }
    var autoRepairPwd by remember(cfg) { mutableStateOf(cfg?.autoRepairPassword ?: false) }
    var lanEnabled by remember(cfg) { mutableStateOf(cfg?.host == "0.0.0.0") }
    var lanBusy by remember { mutableStateOf(false) }
    var lanMsg by remember { mutableStateOf("") }
    var msg by remember { mutableStateOf("") }
    var port by remember { mutableStateOf(GatewayService.PORT) }
    var portText by remember { mutableStateOf("${GatewayService.PORT}") }
    var portBusy by remember { mutableStateOf(false) }
    var portMsg by remember { mutableStateOf("") }
    var updateMsg by remember { mutableStateOf("") }
    var updateBusy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState())
            .padding(16.dp, 16.dp, 16.dp, 32.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Text("设置", fontSize = 24.sp, fontWeight = FontWeight.Bold)

        SectionTitle("调度")
        SectionCard {
            Text("选号策略", color = Muted, fontSize = 13.sp)
            Spacer(Modifier.height(10.dp))
            val options = listOf(
                "balance" to "余额均衡", "daily" to "先用活动余额",
                "permanent" to "先用永久余额", "round_robin" to "顺序轮询",
                "lru" to "最久未用", "weighted" to "加权随机",
            )
            options.chunked(2).forEach { row ->
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp),
                    modifier = Modifier.fillMaxWidth().padding(bottom = 8.dp)) {
                    row.forEach { (key, label) ->
                        StrategyTile(label, key, strategy == key, Modifier.weight(1f)) {
                            strategy = key
                        }
                    }
                }
            }
            Spacer(Modifier.height(6.dp))
            NumberField("上游超时（秒）", timeout) { timeout = it }
            Spacer(Modifier.height(8.dp))
            NumberField("冷却时间（秒）", cooldown) { cooldown = it }
        }

        SectionTitle("会话续期")
        SectionCard {
            NumberField("提前续期（天）", renewDays) { renewDays = it }
            Spacer(Modifier.height(8.dp))
            NumberField("到期前设置密码（天）", pwdDays) { pwdDays = it }
            Spacer(Modifier.height(6.dp))
            Text(
                "无密码账号会在 session 到期前这么多天自动设置一个随机密码，" +
                    "之后每次到期都能用密码自动重登（永久账号）。",
                color = Muted, fontSize = 12.sp,
            )
            Spacer(Modifier.height(12.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("自动设置密码", fontSize = 15.sp)
                    Text("到期前自动给无密码账号设密码（默认关闭）",
                        color = Muted, fontSize = 12.sp)
                }
                Switch(autoSetPwd, { autoSetPwd = it })
            }
            Spacer(Modifier.height(8.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("密码错误时自动改密", fontSize = 15.sp)
                    Text("密码被上游拒绝时用现有 session 重设（默认关闭）",
                        color = Muted, fontSize = 12.sp)
                }
                Switch(autoRepairPwd, { autoRepairPwd = it })
            }
            Spacer(Modifier.height(6.dp))
            Text(
                "这两项都会在账号已登录的其他设备上把 session 顶掉（顶号）。" +
                    "同一账号还在手机客户端登录时建议保持关闭，只在面板里手动设置密码。",
                color = Muted, fontSize = 11.sp,
            )
        }

        SectionTitle("网络与安全")
        SectionCard {
            Text("监听范围", color = Muted, fontSize = 13.sp)
            Spacer(Modifier.height(8.dp))
            Text("当前：${cfg?.host ?: "127.0.0.1"}:${port}",
                fontSize = 13.sp, fontFamily = FontFamily.Monospace)
            Spacer(Modifier.height(10.dp))
            Row(verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    portText, { portText = it },
                    label = { Text("端口") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.weight(1f),
                )
                Button(
                    onClick = {
                        val want = portText.toIntOrNull()
                        if (want == null || want !in 1024..65535) {
                            portMsg = "端口需在 1024–65535 之间"
                            return@Button
                        }
                        if (want == port) { portMsg = "端口未变化"; return@Button }
                        portBusy = true
                        portMsg = "正在切换端口…"
                        scope.launch {
                            val ok = withContext(Dispatchers.IO) {
                                GatewayService.instance?.setPort(want) ?: false
                            }
                            if (!ok) {
                                portMsg = "切换失败（端口被占用？）"
                                portBusy = false
                                return@launch
                            }
                            Api.resetConnections()
                            val ready = Api.waitUntilReady(30_000)
                            if (ready) {
                                port = want
                                portMsg = "已切换到端口 $want"
                                vm.refreshAll()
                            } else {
                                portMsg = "切换超时，请查看运行日志"
                            }
                            portBusy = false
                        }
                    },
                    enabled = !portBusy,
                    shape = RoundedCornerShape(10.dp),
                ) { Text("应用端口", fontSize = 13.sp) }
            }
            if (portMsg.isNotBlank()) {
                Spacer(Modifier.height(6.dp))
                Text(portMsg, fontSize = 12.sp,
                    color = if (portBusy) Muted else Green)
            }
            Text("本包默认端口 ${GatewayService.defaultPort()}（正式版 17991 / 精简版 17992）。" +
                "两个包同时安装时请让它们使用不同端口。",
                color = Muted, fontSize = 11.sp)
            Spacer(Modifier.height(8.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("允许局域网访问", fontSize = 15.sp)
                    Text(
                        if (cfg?.host == "0.0.0.0")
                            "同一 Wi-Fi 下的电脑/其他设备可直连网关"
                        else "仅本机可访问；开启后需重启网关生效",
                        color = Muted, fontSize = 12.sp,
                    )
                }
                Switch(
                    checked = lanEnabled,
                    enabled = !lanBusy,
                    onCheckedChange = { on ->
                        lanEnabled = on
                        lanBusy = true
                        lanMsg = "正在切换监听地址…"
                        scope.launch {
                            val target = if (on) "0.0.0.0" else "127.0.0.1"
                            val ok = withContext(Dispatchers.IO) {
                                GatewayService.instance?.rebind(target) ?: false
                            }
                            if (!ok) {
                                lanEnabled = !on
                                lanMsg = "切换失败（网关未运行？）"
                                lanBusy = false
                                return@launch
                            }
                            // The rebind replaces the Python server object, so
                            // pooled keep-alive sockets still belong to the old
                            // one and would answer with the previous host —
                            // which made the switch flip back.  Drop them, then
                            // wait until the *new* state reports the target.
                            Api.resetConnections()
                            val ready = Api.waitUntilReady(30_000)
                            var confirmed = false
                            if (ready) {
                                repeat(10) {
                                    confirmed = runCatching {
                                        val s = parseState(Api.get("/api/panel/state"))
                                        s.config.host == target
                                    }.getOrDefault(false)
                                    if (!confirmed) {
                                        Api.resetConnections()
                                        kotlinx.coroutines.delay(500)
                                    }
                                }
                            }
                            vm.refreshAll()
                            lanEnabled = confirmed
                            lanMsg = when {
                                !ready -> "切换超时，请查看运行日志"
                                !confirmed -> "切换未生效（状态未同步），请重试"
                                on -> "已开放局域网：http://${localIp() ?: "?"}:${GatewayService.PORT}"
                                else -> "已改为仅本机访问"
                            }
                            lanBusy = false
                        }
                    },
                )
            }
            if (lanMsg.isNotBlank()) {
                Spacer(Modifier.height(6.dp))
                Text(lanMsg, color = if (lanBusy) Muted else Green, fontSize = 12.sp)
            }
            Spacer(Modifier.height(14.dp))
            OutlinedTextField(proxy, { proxy = it },
                label = { Text("全局代理（留空直连）") },
                placeholder = { Text("http://host:port") },
                singleLine = true, modifier = Modifier.fillMaxWidth())
            Spacer(Modifier.height(10.dp))
            OutlinedTextField(apiKeys, { apiKeys = it },
                label = { Text("API Keys（逗号分隔，留空免鉴权）") },
                placeholder = { Text("sk-xxx,sk-yyy") },
                singleLine = true, modifier = Modifier.fillMaxWidth())
        }

        SectionTitle("公网穿透")
        SectionCard {
            TunnelSettings(vm)
        }

        SectionTitle("其他")
        SectionCard {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("记录请求日志", fontSize = 15.sp)
                    Text("写入用量台账与运行日志", color = Muted, fontSize = 12.sp)
                }
                Switch(logRequests, { logRequests = it })
            }
        }

        SectionTitle("更新")
        SectionCard {
            Text("当前版本 ${BuildConfig.VERSION_NAME}（${if (BuildConfig.ENABLE_SMS_REGISTRATION) "接码版" else "精简版"}）",
                fontSize = 13.sp)
            Spacer(Modifier.height(8.dp))
            Button(
                onClick = {
                    if (updateBusy) return@Button
                    updateBusy = true
                    updateMsg = "正在检查…"
                    scope.launch {
                        try {
                            updateMsg = vm.checkUpdate()
                        } finally {
                            updateBusy = false
                        }
                    }
                },
                enabled = !updateBusy,
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(10.dp),
            ) { Text("检查更新", fontSize = 13.sp) }
            Spacer(Modifier.height(6.dp))
            Text("更新包来自公开仓库 706412584/loomy2api-releases，无需 Token。",
                color = Muted, fontSize = 11.sp)
            if (updateMsg.contains("发现新版本")) {
                Spacer(Modifier.height(8.dp))
                Button(
                    onClick = {
                        if (updateBusy) return@Button
                        updateBusy = true
                        updateMsg = "正在下载…"
                        scope.launch {
                            try {
                                updateMsg = vm.installUpdate { pct ->
                                    updateMsg = "正在下载… $pct%"
                                }
                            } finally {
                                updateBusy = false
                            }
                        }
                    },
                    enabled = !updateBusy,
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(10.dp),
                ) { Text("下载并安装") }
            }
            if (updateBusy) {
                Spacer(Modifier.height(8.dp))
                LinearProgressIndicator(Modifier.fillMaxWidth(), color = Blue)
            }
            if (updateMsg.isNotBlank()) {
                Spacer(Modifier.height(6.dp))
                Text(updateMsg, fontSize = 12.sp,
                    color = if (updateMsg.contains("失败") || updateMsg.contains("无法"))
                        Red else Muted)
            }
        }

        Button(
            onClick = {
                scope.launch {
                    vm.saveConfig(mapOf(
                        "strategy" to strategy,
                        "proxy" to proxy,
                        "api_keys" to apiKeys.split(",").map { it.trim() }.filter { it.isNotEmpty() },
                        "log_requests" to logRequests,
                        "timeout" to (timeout.toIntOrNull() ?: 1200),
                        "cooldown_seconds" to (cooldown.toIntOrNull() ?: 300),
                        "session_renew_before_days" to (renewDays.toIntOrNull() ?: 3),
                        "password_set_before_days" to (pwdDays.toIntOrNull() ?: 1),
                        "auto_set_password" to autoSetPwd,
                        "auto_repair_password" to autoRepairPwd,
                    )) { msg = it }
                }
            },
            modifier = Modifier.fillMaxWidth().height(50.dp),
            shape = RoundedCornerShape(12.dp),
        ) { Text("保存配置", fontSize = 15.sp) }

        if (msg.isNotBlank()) {
            Text(msg, fontSize = 13.sp, color = if (msg.contains("失败")) Red else Green)
        }

        Spacer(Modifier.height(6.dp))
        Text("Loomy2API for Android · 核心链路移植版", color = Muted, fontSize = 12.sp)
        Text("账号数据与配置保存在应用私有目录，卸载即清除。",
            color = Muted, fontSize = 12.sp)
    }
}

@Composable
private fun TunnelSettings(vm: GatewayViewModel) {
    val ctx = AssetBridge.appContext
    val saved = remember { ctx?.let { TunnelManager.loadSettings(it) } ?: TunnelManager.Settings() }
    val tunnel by vm.tunnel.collectAsState()

    var mode by remember { mutableStateOf(saved.mode) }
    var token by remember { mutableStateOf(saved.token) }
    var domain by remember { mutableStateOf(saved.domain) }
    var tokenVisible by remember { mutableStateOf(false) }

    val busy = tunnel.status == TunnelStatus.STARTING ||
        tunnel.status == TunnelStatus.RECONNECTING
    val running = tunnel.status == TunnelStatus.RUNNING
    // TunnelManager reports the persisted intent, so the switch stays on while
    // the service is reconnecting after a restart.
    val enabled = tunnel.enabled

    Row(verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text("公网访问（Cloudflare Tunnel）", fontSize = 15.sp)
            Text(
                "通过 cloudflared 把本机网关暴露到公网；无需公网 IP。",
                color = Muted, fontSize = 12.sp,
            )
        }
        Switch(
            checked = enabled,
            // Always allow switching OFF, including mid-reconnect: a tunnel
            // that never connects must remain cancellable, otherwise a wrong
            // token leaves the user with no way out.
            enabled = enabled || !busy,
            onCheckedChange = { on ->
                if (!on) {
                    vm.stopTunnel()
                } else {
                    // Only the token is required. Cloudflare's wizard needs the
                    // tunnel to connect before a Public Hostname can be added,
                    // so the domain is usually filled in afterwards.
                    if (mode == TunnelMode.NAMED && token.isBlank()) {
                        vm.toast("请先填写 Tunnel Token")
                    } else {
                        vm.startTunnel(mode, token, domain)
                    }
                }
            },
        )
    }

    Spacer(Modifier.height(10.dp))
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        FilterChip(
            selected = mode == TunnelMode.QUICK,
            onClick = { mode = TunnelMode.QUICK },
            label = { Text("临时公网地址") },
            enabled = !busy && !running,
        )
        FilterChip(
            selected = mode == TunnelMode.NAMED,
            onClick = { mode = TunnelMode.NAMED },
            label = { Text("指定域名") },
            enabled = !busy && !running,
        )
    }

    if (mode == TunnelMode.NAMED) {
        Spacer(Modifier.height(10.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(
                token, { token = it },
                label = { Text("Tunnel Token") },
                placeholder = { Text("Zero Trust → Tunnels → 安装命令里的 token") },
                singleLine = true,
                visualTransformation = if (tokenVisible) androidx.compose.ui.text.input.VisualTransformation.None
                    else PasswordVisualTransformation(),
                trailingIcon = {
                    TextButton(onClick = { tokenVisible = !tokenVisible }) {
                        Text(if (tokenVisible) "隐藏" else "显示", fontSize = 12.sp)
                    }
                },
                enabled = !busy && !running,
                modifier = Modifier.weight(1f),
            )
            Spacer(Modifier.width(8.dp))
            Button(
                onClick = {
                    if (token.isBlank()) {
                        vm.toast("请先填写 Tunnel Token")
                    } else {
                        vm.startTunnel(TunnelMode.NAMED, token, domain)
                    }
                },
                enabled = !busy && !running,
                shape = RoundedCornerShape(10.dp),
                modifier = Modifier.height(56.dp),
            ) {
                Text(
                    when {
                        running -> "已连接"
                        busy -> "验证中…"
                        else -> "验证连接"
                    },
                    fontSize = 13.sp,
                )
            }
        }
        Spacer(Modifier.height(4.dp))
        Text(
            "点「验证连接」会立即用该 token 建立隧道；Cloudflare 向导显示已连接后，" +
                "即可继续下一步添加域名。",
            color = Muted, fontSize = 11.sp,
        )
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            domain, { domain = it },
            label = { Text("公网域名（可稍后填写）") },
            placeholder = { Text("api.example.com") },
            singleLine = true,
            enabled = !busy,
            trailingIcon = {
                if (running && domain.isNotBlank() && domain != tunnel.url?.removePrefix("https://")) {
                    TextButton(onClick = { vm.updateTunnelDomain(domain) }) {
                        Text("应用", fontSize = 12.sp)
                    }
                }
            },
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(6.dp))
        Text(
            "首次使用顺序：填 token → 打开开关（状态变为「已连接」，Cloudflare " +
                "向导即可继续）→ 在 Zero Trust 里给该隧道添加 Public Hostname，指向 " +
                "http://127.0.0.1:${GatewayService.PORT} → 回到这里填域名。",
            color = Muted, fontSize = 11.sp,
        )
    }

    Spacer(Modifier.height(10.dp))
    val statusText = when (tunnel.status) {
        TunnelStatus.IDLE -> "未启动"
        TunnelStatus.STARTING -> "正在建立隧道…"
        TunnelStatus.RUNNING -> "已连接"
        TunnelStatus.RECONNECTING -> "正在重连…"
        TunnelStatus.ERROR -> "错误"
    }
    Text(
        "状态：$statusText",
        fontSize = 13.sp,
        color = when (tunnel.status) {
            TunnelStatus.RUNNING -> Green
            TunnelStatus.ERROR -> Red
            else -> Muted
        },
    )
    tunnel.error?.takeIf { tunnel.status != TunnelStatus.RUNNING }?.let {
        Spacer(Modifier.height(4.dp))
        Text(it, fontSize = 11.sp, color = Muted, maxLines = 3, overflow = TextOverflow.Ellipsis)
    }
    tunnel.url?.let { url ->
        Spacer(Modifier.height(8.dp))
        CopyRow("公网 Base URL", url)
        Text(
            "OpenAI: $url/v1 · Anthropic: $url",
            fontSize = 11.sp, color = Muted,
        )
    }

    if (tunnel.status == TunnelStatus.IDLE && !tunnel.enabled) {
        Spacer(Modifier.height(6.dp))
        Text(
            "开启后若尚未设置 API Key，会自动生成一个，避免公网裸奔。",
            color = Muted, fontSize = 11.sp,
        )
    }
}

@Composable
private fun StrategyTile(label: String, key: String, selected: Boolean,
                         modifier: Modifier, onClick: () -> Unit) {
    Column(
        modifier
            .background(if (selected) BlueSoft else Color.Transparent, RoundedCornerShape(10.dp))
            .clickable(onClick = onClick)
            .padding(12.dp)
    ) {
        Text(label, fontSize = 14.sp,
            color = if (selected) Blue else Ink,
            fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal)
        Text(key, color = Muted, fontSize = 11.sp)
    }
}

@Composable
private fun NumberField(label: String, value: String, onChange: (String) -> Unit) {
    OutlinedTextField(value, onChange, label = { Text(label) },
        singleLine = true,
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
        modifier = Modifier.fillMaxWidth())
}

// ================================================================ helpers

private fun formatTokens(v: Int): String = when {
    v >= 1_000_000 -> "%.1fM".format(v / 1_000_000.0)
    v >= 1_000 -> "%.1fK".format(v / 1_000.0)
    else -> "$v"
}

private fun formatTime(ts: Long): String =
    SimpleDateFormat("MM-dd HH:mm", Locale.getDefault()).format(Date(ts * 1000))

private fun copyToClipboard(text: String) {
    val ctx = AssetBridge.appContext ?: return
    try {
        val cm = ctx.getSystemService(android.content.Context.CLIPBOARD_SERVICE)
                as android.content.ClipboardManager
        cm.setPrimaryClip(android.content.ClipData.newPlainText("loomy2api", text))
    } catch (_: Throwable) {
        // clipboard access can be denied in the background; the text is still shown
    }
}

private fun localIp(): String? {
    return try {
        java.net.NetworkInterface.getNetworkInterfaces().toList()
            .flatMap { it.inetAddresses.toList() }
            .firstOrNull {
                !it.isLoopbackAddress && it is java.net.Inet4Address
            }?.hostAddress
    } catch (_: Exception) {
        null
    }
}
