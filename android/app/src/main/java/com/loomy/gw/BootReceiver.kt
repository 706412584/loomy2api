package com.loomy.gw

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Bring the gateway back after a reboot, an app update, or a vendor "quick
 * boot".  Without this the phone silently stops serving after every restart.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED,
            "android.intent.action.QUICKBOOT_POWERON",
            "com.htc.intent.action.QUICKBOOT_POWERON" -> GatewayService.start(context)
        }
    }
}
