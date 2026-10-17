package com.loomy.gw

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Restart the gateway after boot (user opt-in via battery whitelist anyway). */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            GatewayService.start(context)
        }
    }
}
