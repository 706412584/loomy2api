package com.loomy.gw

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Foreground service that boots the CPython interpreter (Chaquopy) and runs the
 * loomy2api gateway inside the app process.
 *
 * ## Why the extra machinery
 *
 * A plain foreground service keeps the *process* at foreground priority but does
 * not stop the CPU from suspending when the screen turns off: the Python threads
 * are then frozen and every in-flight request dies.  This service therefore
 *
 *  1. holds a `PARTIAL_WAKE_LOCK` for its whole lifetime (released in
 *     `onDestroy`; the kernel reclaims it if the process is killed),
 *  2. runs a watchdog that probes `/health` every [HEALTH_INTERVAL_MS] and asks
 *     Python to restart the HTTP server if it stops answering,
 *  3. comes back on `START_STICKY` and re-boots the gateway when the process was
 *     killed in the background,
 *  4. survives "swipe the task away" via `onTaskRemoved`.
 */
class GatewayService : Service() {

    companion object {
        const val CHANNEL = "gateway"

        /**
         * Listening port.  Mutable because the user can change it in Settings
         * and because the lite build defaults to a different port so both
         * builds can run on one phone at the same time.
         *
         * Deliberately different from the desktop gateway's 17990.
         */
        @Volatile var PORT: Int = 17991
            private set

        /** Default for this flavor: full 17991, lite 17992 (co-existence). */
        fun defaultPort(): Int =
            if (BuildConfig.ENABLE_SMS_REGISTRATION) 17991 else 17992

        private const val PREFS = "loomy_settings"
        private const val KEY_PORT = "port"

        /** Stored port for this app (full and lite have separate storage). */
        fun loadPort(context: Context): Int {
            val stored = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .getInt(KEY_PORT, 0)
            return if (stored in 1024..65535) stored else defaultPort()
        }

        fun savePort(context: Context, port: Int) {
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit().putInt(KEY_PORT, port).apply()
            PORT = port
        }

        private const val NOTIFICATION_ID = 1
        private const val WAKE_LOCK_TAG = "loomy2api:gateway"
        private const val ACTION_STOP = "com.loomy.gw.action.STOP_GATEWAY"

        /** How often the watchdog probes /health. */
        private const val HEALTH_INTERVAL_MS = 30_000L
        /** Consecutive failures before we consider the gateway dead. */
        private const val HEALTH_FAILURES_BEFORE_RESTART = 2

        @Volatile var instance: GatewayService? = null

        /** Start the gateway service; safe to call repeatedly. */
        fun start(context: Context) {
            val intent = Intent(context, GatewayService::class.java)
            try {
                if (Build.VERSION.SDK_INT >= 26) {
                    context.startForegroundService(intent)
                } else {
                    context.startService(intent)
                }
            } catch (t: Throwable) {
                // Some ROMs refuse a background startForegroundService.
                android.util.Log.w("GatewayService", "start failed", t)
            }
        }

        fun stop(context: Context) {
            try {
                context.stopService(Intent(context, GatewayService::class.java))
            } catch (t: Throwable) {
                android.util.Log.w("GatewayService", "stop failed", t)
            }
        }

        /**
         * The gateway's configured API key, read from the live Python config.
         *
         * The UI cannot learn it over HTTP when auth is enabled: the panel
         * endpoints are exactly what the key protects. Keeping a copy in
         * SharedPreferences covers the normal case, but a key configured by the
         * CLI/desktop gateway (or before this build was installed) would leave
         * the app locked out. Python owns the config, so ask it directly.
         *
         * Blocking call — invoke from a background dispatcher.
         */
        fun readApiKey(ctx: Context): String = try {
            if (!Python.isStarted()) {
                Python.start(AndroidPlatform(ctx.applicationContext))
            }
            Python.getInstance().getModule("mobile_main").callAttr("current_api_key")
                .toString().orEmpty()
        } catch (t: Throwable) {
            android.util.Log.w("GatewayService", "readApiKey failed", t)
            ""
        }
    }

    private var wakeLock: PowerManager.WakeLock? = null
    private val watchdog = Executors.newSingleThreadScheduledExecutor { r ->
        Thread(r, "gateway-watchdog").apply { isDaemon = true }
    }
    private val booting = AtomicBoolean(false)
    private var failures = 0
    @Volatile private var stopping = false

    override fun onCreate() {
        super.onCreate()
        AssetBridge.appContext = applicationContext
        PORT = loadPort(applicationContext)
        createChannel()

        // Promote to foreground *first*: startForegroundService must reach
        // startForeground within ~5 s, and Python.start() plus the package
        // unpack can exceed that on a cold start.
        startForeground(NOTIFICATION_ID, buildNotification())
        instance = this

        acquireWakeLock()
        ensureGatewayRunning()
        startWatchdog()
        TunnelManager.restoreIfEnabled(applicationContext, PORT)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopping = true
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            return START_NOT_STICKY
        }
        // A START_STICKY restart lands here after the process was killed; the
        // Python interpreter and its threads are gone, so boot it again.
        ensureGatewayRunning()
        return START_STICKY
    }

    /**
     * Swiping the task away must not take the gateway down — it is a server for
     * other devices.  Restart the service so it keeps running headless.
     */
    override fun onTaskRemoved(rootIntent: Intent?) {
        if (!stopping) {
            try {
                if (Build.VERSION.SDK_INT >= 26) {
                    startForegroundService(Intent(this, GatewayService::class.java))
                } else {
                    startService(Intent(this, GatewayService::class.java))
                }
            } catch (t: Throwable) {
                android.util.Log.w("GatewayService", "restart after task removal failed", t)
            }
        }
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        stopping = true
        // Do not leave cloudflared orphaned when the service goes away. The
        // saved "enabled" flag is kept so a restart reconnects it.
        TunnelManager.shutdown()
        try { watchdog.shutdownNow() } catch (_: Throwable) {}
        releaseWakeLock()
        try { stopForeground(STOP_FOREGROUND_REMOVE) } catch (_: Throwable) {}
        instance = null
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // ------------------------------------------------------------ gateway

    /** Boot the Python gateway (idempotent: mobile_main only binds once). */
    private fun ensureGatewayRunning() {
        if (!booting.compareAndSet(false, true)) return
        Thread({
            try {
                if (!Python.isStarted()) {
                    Python.start(AndroidPlatform(applicationContext))
                }
                val mod = Python.getInstance().getModule("mobile_main")
                mod.callAttr("start_gateway", filesDir.absolutePath, PORT)
                android.util.Log.i("GatewayService", "gateway boot requested")
            } catch (t: Throwable) {
                android.util.Log.e("GatewayService", "gateway boot failed", t)
            } finally {
                booting.set(false)
            }
        }, "gateway-boot").start()
    }

    /**
     * Rebind the gateway on another interface (127.0.0.1 <-> 0.0.0.0).
     *
     * Python persists the choice to config.json and restarts its HTTP server;
     * the caller waits for /health to come back before refreshing the UI.
     */
    fun rebind(host: String): Boolean {
        return try {
            if (!Python.isStarted()) {
                Python.start(AndroidPlatform(applicationContext))
            }
            Python.getInstance().getModule("mobile_main")
                .callAttr("bind_host", host, PORT)
            true
        } catch (t: Throwable) {
            android.util.Log.e("GatewayService", "rebind to $host failed", t)
            false
        }
    }

    /**
     * Change the listening port and restart the gateway on it.
     *
     * The port is persisted per app (full and lite keep separate prefs), so
     * two builds can run side by side without fighting over one socket.
     * Returns false when the new port could not be bound — the caller should
     * then restore the previous value.
     */
    fun setPort(port: Int): Boolean {
        if (port !in 1024..65535) return false
        val previous = PORT
        return try {
            if (!Python.isStarted()) {
                Python.start(AndroidPlatform(applicationContext))
            }
            PORT = port
            Python.getInstance().getModule("mobile_main")
                .callAttr("rebind_port", port)
            savePort(applicationContext, port)
            // An enabled tunnel points at the old port; restart it.
            TunnelManager.restartForPort(applicationContext, port)
            true
        } catch (t: Throwable) {
            android.util.Log.e("GatewayService", "setPort($port) failed", t)
            PORT = previous
            false
        }
    }

    /** Probe /health; restart the gateway after repeated failures. */
    private fun startWatchdog() {
        watchdog.scheduleWithFixedDelay({
            if (stopping) return@scheduleWithFixedDelay
            if (probeHealth()) {
                failures = 0
                return@scheduleWithFixedDelay
            }
            failures++
            if (failures < HEALTH_FAILURES_BEFORE_RESTART) return@scheduleWithFixedDelay
            failures = 0
            android.util.Log.w("GatewayService", "gateway unresponsive, restarting")
            try {
                Python.getInstance().getModule("mobile_main").callAttr("restart_gateway")
            } catch (t: Throwable) {
                android.util.Log.e("GatewayService", "restart_gateway failed", t)
                ensureGatewayRunning()
            }
        }, HEALTH_INTERVAL_MS, HEALTH_INTERVAL_MS, TimeUnit.MILLISECONDS)
    }

    private fun probeHealth(): Boolean = try {
        val conn = URL("http://127.0.0.1:$PORT/health").openConnection() as HttpURLConnection
        conn.connectTimeout = 2000
        conn.readTimeout = 2000
        val ok = conn.responseCode == 200
        conn.disconnect()
        ok
    } catch (_: Throwable) {
        false
    }

    // --------------------------------------------------------- wake lock

    private fun acquireWakeLock() {
        if (wakeLock != null) return
        val pm = getSystemService(Context.POWER_SERVICE) as? PowerManager ?: return
        try {
            // No timeout: the service releases it in onDestroy.  A timed lock
            // would silently expire mid-request and freeze the gateway again.
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, WAKE_LOCK_TAG)
                .also { it.setReferenceCounted(false); it.acquire() }
        } catch (t: Throwable) {
            android.util.Log.w("GatewayService", "acquire wakelock failed", t)
        }
    }

    private fun releaseWakeLock() {
        val lock = wakeLock ?: return
        wakeLock = null
        try {
            if (lock.isHeld) lock.release()
        } catch (_: Throwable) {
            // the kernel reclaims it when the process dies
        }
    }

    // ------------------------------------------------------- notification

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = getSystemService(NotificationManager::class.java)
        val channel = NotificationChannel(CHANNEL, "网关", NotificationManager.IMPORTANCE_LOW)
        channel.description = "本地 API 网关运行状态"
        channel.setShowBadge(false)
        nm.createNotificationChannel(channel)
    }

    private fun buildNotification(): Notification {
        val launch = packageManager.getLaunchIntentForPackage(packageName)
        val contentIntent = launch?.let {
            PendingIntent.getActivity(this, 0, it,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        }
        val stopIntent = PendingIntent.getService(
            this, 1,
            Intent(this, GatewayService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)

        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle(getString(R.string.notif_title))
            .setContentText(getString(R.string.notif_text))
            .setContentIntent(contentIntent)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel,
                "停止网关", stopIntent)
            .build()
    }
}
