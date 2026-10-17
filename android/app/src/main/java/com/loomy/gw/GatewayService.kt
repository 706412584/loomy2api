package com.loomy.gw

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.IBinder
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/**
 * Foreground service that boots the CPython interpreter (Chaquopy) and runs
 * the original loomy2api Python gateway inside the app process.
 */
class GatewayService : Service() {
    companion object {
        const val CHANNEL = "gateway"

        /** Deliberately different from the desktop gateway's 17990 so both can
         *  run side by side (and so adb-forwarded ports never collide). */
        const val PORT = 17991

        @Volatile var instance: GatewayService? = null

        /** Start the gateway service; safe to call repeatedly (START_STICKY). */
        fun start(context: android.content.Context) {
            val intent = Intent(context, GatewayService::class.java)
            if (android.os.Build.VERSION.SDK_INT >= 26) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }
    }

    override fun onCreate() {
        super.onCreate()
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "网关",
            NotificationManager.IMPORTANCE_LOW))

        AssetBridge.appContext = this

        // Promote to foreground *before* booting Python: startForegroundService
        // must call startForeground within ~5 s, and Python.start() plus the
        // package unpack can exceed that on a cold start (Android 8+ then
        // kills the service with ForegroundServiceDidNotStartInTimeException).
        val notif = Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle(getString(R.string.notif_title))
            .setContentText(getString(R.string.notif_text))
            .setOngoing(true)
        startForeground(1, notif.build())
        instance = this

        // Python boot is slow; keep it off the main thread so the service
        // callback returns promptly.
        Thread({
            try {
                if (!Python.isStarted()) {
                    Python.start(AndroidPlatform(this))
                }
                val py = Python.getInstance()
                val mod = py.getModule("mobile_main")
                mod.callAttr("start_gateway", filesDir.absolutePath, PORT)
            } catch (t: Throwable) {
                android.util.Log.e("GatewayService", "gateway boot failed", t)
            }
        }, "gateway-boot").start()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int =
        START_STICKY

    override fun onDestroy() {
        // CPython threads are daemon threads tied to the process; the process
        // dies with the service, nothing else to stop here.
        instance = null
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
