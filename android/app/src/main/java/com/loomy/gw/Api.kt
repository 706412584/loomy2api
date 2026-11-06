package com.loomy.gw

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/** Minimal JSON-over-HTTP client for the in-process Python gateway. */
object Api {
    private val client = OkHttpClient.Builder()
        // The gateway is in-process: never let a system proxy / VPN (Clash,
        // v2rayNG, …) intercept loopback traffic.  A TUN-mode proxy answers
        // our requests with HTTP CONNECT, which the Python handler does not
        // implement and replies to with 501 — previously a crash.
        .proxy(java.net.Proxy.NO_PROXY)
        .connectTimeout(3, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()

    private val JSON = "application/json; charset=utf-8".toMediaType()

    /**
     * Drop pooled connections.
     *
     * The gateway runs in this process, so a rebind replaces the Python HTTP
     * server but leaves OkHttp's keep-alive sockets attached to the *old*
     * server object.  Requests then kept being answered with stale state
     * (e.g. the LAN toggle flipping back) until the pool expired.  The caller
     * invokes this after a rebind and before the next state read.
     */
    fun resetConnections() {
        try {
            client.connectionPool.evictAll()
        } catch (_: Throwable) {
            // best effort: a failed eviction only costs one stale response
        }
    }

    /**
     * API key for the gateway's own gate.  The panel HTML is exempt from the
     * key check, but the panel JSON endpoints are not, so once the user
     * configures `api_keys` every request must carry it or the UI locks
     * itself out.
     */
    @Volatile var apiKey: String = ""
        set(value) {
            field = value
            val ctx = AssetBridge.appContext ?: return
            ctx.getSharedPreferences(PREFS, android.content.Context.MODE_PRIVATE)
                .edit().putString(KEY_API_KEY, value).apply()
        }

    private const val PREFS = "loomy_settings"
    private const val KEY_API_KEY = "api_key"

    /**
     * Restore the key persisted by a previous process.
     *
     * The key used to live only in memory, so after a restart the UI sent no
     * key, every panel request answered 401 ("无效的 API Key"), and the UI
     * could not recover — reading the state is what would have supplied it.
     */
    fun loadStoredKey(ctx: android.content.Context) {
        val stored = ctx.getSharedPreferences(PREFS, android.content.Context.MODE_PRIVATE)
            .getString(KEY_API_KEY, "").orEmpty()
        if (stored.isNotEmpty()) apiKey = stored
    }

    private fun base() = "http://127.0.0.1:${GatewayService.PORT}"

    private fun Request.Builder.auth(): Request.Builder = apply {
        val key = apiKey.trim()
        if (key.isNotEmpty()) header("Authorization", "Bearer $key")
    }

    suspend fun get(path: String, timeoutSec: Long = 120): JSONObject =
        withContext(Dispatchers.IO) {
            val req = Request.Builder().url(base() + path).auth().get().build()
            client.newBuilder().readTimeout(timeoutSec, TimeUnit.SECONDS).build()
                .newCall(req).execute().use { resp ->
                    val body = resp.body?.string() ?: "{}"
                    if (!resp.isSuccessful) throw ApiException(resp.code, errorMessage(body, resp.code))
                    JSONObject(body)
                }
        }

    suspend fun post(path: String, body: JSONObject = JSONObject(),
                     timeoutSec: Long = 120): JSONObject =
        withContext(Dispatchers.IO) {
            val req = Request.Builder()
                .url(base() + path)
                .auth()
                .post(body.toString().toRequestBody(JSON))
                .build()
            client.newBuilder().readTimeout(timeoutSec, TimeUnit.SECONDS).build()
                .newCall(req).execute().use { resp ->
                val text = resp.body?.string() ?: "{}"
                if (!resp.isSuccessful) throw ApiException(resp.code, errorMessage(text, resp.code))
                JSONObject(text)
            }
        }

    /** Wait until the gateway answers /health (the Python boot is async). */
    suspend fun waitUntilReady(maxMillis: Long = 45_000): Boolean =
        withContext(Dispatchers.IO) {
            val deadline = System.currentTimeMillis() + maxMillis
            while (System.currentTimeMillis() < deadline) {
                try {
                    val req = Request.Builder().url(base() + "/health").get().build()
                    client.newBuilder()
                        .connectTimeout(1, TimeUnit.SECONDS)
                        .readTimeout(1, TimeUnit.SECONDS).build()
                        .newCall(req).execute().use { if (it.isSuccessful) return@withContext true }
                } catch (_: Exception) {
                    // not up yet
                }
                kotlinx.coroutines.delay(250)
            }
            false
        }

    private fun errorMessage(body: String, code: Int): String = try {
        val obj = JSONObject(body)
        obj.optJSONObject("error")?.optString("message").takeUnless { it.isNullOrBlank() }
            ?: obj.optString("error").takeUnless { it.isNullOrBlank() }
            ?: "HTTP $code"
    } catch (_: Exception) {
        "HTTP $code"
    }
}

class ApiException(val status: Int, message: String) : Exception(message)

// ---------------------------------------------------------------- models

data class AccountView(
    val name: String,
    val loginidMasked: String,
    val enabled: Boolean,
    val hasSession: Boolean,
    val inCooldown: Boolean,
    val cooldownLeft: Double,
    val sessionDaysLeft: Double?,
    val available: Int?,
    val balance: Int?,
    val dailyBalance: Int?,
    val requests: Int,
    val failures: Int,
    val pointsUsed: Int,
    val devid: String,
    val mode: String,
    val lastError: String,
    val source: String,
    val permanent: Boolean,
    val passwordSetAt: Long,
    val passwordVerifiedAt: Long,
    /** null = not queried yet (show 未知, not 未激活). */
    val activated: Boolean? = null,
    val appliedInvitationCode: String = "",
    /** when the account entered this pool (unix seconds). */
    val obtainedAt: Long = 0,
    /** lifetime ledger row count — a fresh number has few, a recycled one thousands. */
    val ledgerTotal: Int? = null,
    /** true when registration detected a pre-existing (recycled) number. */
    val recycled: Boolean? = null,
    /** true when the upstream disabled this account (banned / suspended). */
    val banned: Boolean = false,
) {
    val statusLabel: String
        get() = when {
            !enabled -> "已禁用"
            banned -> "已封禁"
            inCooldown -> "冷却中"
            !hasSession -> "无登录态"
            // a stored password means expiry is handled by an automatic login
            permanent -> if (sessionDaysLeft != null && sessionDaysLeft < 3) "永久" else "正常"
            sessionDaysLeft != null && sessionDaysLeft < 3 -> "即将到期"
            available == 0 -> "额度用尽"
            else -> "正常"
        }
    val healthy: Boolean
        get() = enabled && hasSession && !inCooldown
}

data class Totals(
    val accounts: Int, val usable: Int, val available: Int,
    val requests: Int, val pointsUsed: Int,
    val totalRequests: Int, val tokens: Int, val failures: Int,
)

data class ConfigView(
    val upstream: String,
    val strategy: String,
    val defaultModel: String,
    val identityMode: String,
    val models: Int,
    val authRequired: Boolean,
    val host: String,
    val port: Int,
    val apiKeys: List<String>,
    val proxy: String,
    val logRequests: Boolean,
    val timeout: Int,
    val maxRetries: Int,
    val cooldownSeconds: Int,
    val sessionRenewBeforeDays: Int,
    val quotaRefreshMinutes: Int,
    val sessionsFromClient: Boolean,
    val passwordSetBeforeDays: Int,
    // password automation ("顶号" risk) — off by default on mobile
    val autoSetPassword: Boolean = false,
    val autoRepairPassword: Boolean = false,
    // fysms 接码平台: the username is shown, the password never travels here
    val fysmsUsername: String = "",
    val fysmsPasswordSet: Boolean = false,
    val fysmsKeyword: String = "讯飞",
    val smsRegistration: Boolean = true,
)

data class StateView(
    val accounts: List<AccountView>,
    val totals: Totals,
    val config: ConfigView,
)

data class ModelView(
    val id: String, val name: String, val multiplier: Double?,
    val contextLength: Int?, val maxOutput: Int?, val type: String,
    val reasoning: Boolean, val vision: Boolean, val tools: Boolean,
    val isDefault: Boolean,
)

data class CallRecord(
    val ts: Long, val account: String, val model: String, val status: Int,
    val ok: Boolean, val kind: String, val stream: Boolean, val seconds: Double,
    val tokens: Int, val points: Int, val error: String,
)

// ---------------------------------------------------------------- parsing

fun parseState(o: JSONObject): StateView {
    val t = o.optJSONObject("totals") ?: JSONObject()
    val c = o.optJSONObject("config") ?: JSONObject()
    val arr = o.optJSONArray("accounts") ?: JSONArray()
    val accounts = (0 until arr.length()).map { i ->
        val a = arr.getJSONObject(i)
        val id = a.optJSONObject("identity") ?: JSONObject()
        AccountView(
            name = a.optString("name"),
            loginidMasked = a.optString("loginid_masked"),
            enabled = a.optBoolean("enabled", true),
            hasSession = a.optString("session").isNotEmpty(),
            inCooldown = a.optBoolean("in_cooldown"),
            cooldownLeft = a.optDouble("cooldown_seconds_left", 0.0),
            sessionDaysLeft = if (a.isNull("session_days_left")) null
                else a.optDouble("session_days_left"),
            available = if (a.isNull("available")) null else a.optInt("available"),
            balance = if (a.isNull("balance")) null else a.optInt("balance"),
            dailyBalance = if (a.isNull("daily_balance")) null else a.optInt("daily_balance"),
            requests = a.optInt("requests"),
            failures = a.optInt("failures"),
            pointsUsed = a.optInt("points_used"),
            devid = id.optString("devid"),
            mode = a.optString("mode"),
            lastError = a.optString("last_error"),
            source = a.optString("source"),
            permanent = a.optBoolean("permanent"),
            passwordSetAt = a.optLong("password_set_at"),
            passwordVerifiedAt = a.optLong("password_verified_at"),
            activated = if (a.isNull("activated")) null else a.optBoolean("activated"),
            appliedInvitationCode = a.optString("applied_invitation_code"),
            obtainedAt = a.optLong("obtained_at"),
            ledgerTotal = if (a.isNull("ledger_total")) null else a.optInt("ledger_total"),
            recycled = if (a.isNull("recycled")) null else a.optBoolean("recycled"),
            banned = a.optBoolean("banned"),
        )
    }
    val keys = c.optJSONArray("api_keys") ?: JSONArray()
    return StateView(
        accounts = accounts,
        totals = Totals(
            accounts = t.optInt("accounts"),
            usable = t.optInt("usable"),
            available = t.optInt("available"),
            requests = t.optInt("requests"),
            pointsUsed = t.optInt("points_used"),
            totalRequests = t.optInt("total_requests", t.optInt("requests")),
            tokens = t.optInt("tokens"),
            failures = t.optInt("failures"),
        ),
        config = ConfigView(
            upstream = c.optString("upstream"),
            strategy = c.optString("strategy", "balance"),
            defaultModel = c.optString("default_model"),
            identityMode = c.optString("identity_mode"),
            models = c.optInt("models"),
            authRequired = c.optBoolean("auth_required"),
            host = c.optString("host", "127.0.0.1"),
            port = c.optInt("port", GatewayService.PORT),
            apiKeys = (0 until keys.length()).map { keys.optString(it) },
            proxy = c.optString("proxy"),
            logRequests = c.optBoolean("log_requests", true),
            timeout = c.optInt("timeout", 1200),
            maxRetries = c.optInt("max_retries", 2),
            cooldownSeconds = c.optInt("cooldown_seconds", 300),
            sessionRenewBeforeDays = c.optInt("session_renew_before_days", 3),
            quotaRefreshMinutes = c.optInt("quota_refresh_minutes", 30),
            sessionsFromClient = c.optBoolean("sessions_from_client", true),
            passwordSetBeforeDays = c.optInt("password_set_before_days", 1),
            autoSetPassword = c.optBoolean("auto_set_password", false),
            autoRepairPassword = c.optBoolean("auto_repair_password", false),
            fysmsUsername = c.optString("fysms_username"),
            fysmsPasswordSet = c.optBoolean("fysms_password_set"),
            fysmsKeyword = c.optString("fysms_keyword", "讯飞"),
            smsRegistration = c.optBoolean("sms_registration", true),
        ),
    )
}

fun parseModels(o: JSONObject): List<ModelView> {
    val arr = o.optJSONArray("models") ?: JSONArray()
    return (0 until arr.length()).map { i ->
        val m = arr.getJSONObject(i)
        ModelView(
            id = m.optString("id"),
            name = m.optString("name"),
            multiplier = if (m.isNull("multiplier")) null else m.optDouble("multiplier"),
            contextLength = if (m.isNull("context_length")) null else m.optInt("context_length"),
            maxOutput = if (m.isNull("max_output_tokens")) null else m.optInt("max_output_tokens"),
            type = m.optString("type", "chat"),
            reasoning = m.optBoolean("reasoning"),
            vision = m.optBoolean("vision"),
            tools = m.optBoolean("function_calling"),
            isDefault = m.optBoolean("default_model"),
        )
    }
}

fun parseRecords(o: JSONObject): Pair<List<CallRecord>, JSONObject> {
    val arr = o.optJSONArray("rows") ?: JSONArray()
    val rows = (0 until arr.length()).map { i ->
        val r = arr.getJSONObject(i)
        CallRecord(
            ts = r.optLong("ts"),
            account = r.optString("account"),
            model = r.optString("model"),
            status = r.optInt("status"),
            ok = r.optBoolean("ok"),
            kind = r.optString("kind"),
            stream = r.optBoolean("stream"),
            seconds = r.optDouble("seconds"),
            tokens = r.optInt("tokens"),
            points = r.optInt("points"),
            error = r.optString("error"),
        )
    }
    return rows to (o.optJSONObject("totals") ?: JSONObject())
}
