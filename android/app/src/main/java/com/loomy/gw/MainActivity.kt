package com.loomy.gw

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.lifecycle.viewmodel.compose.viewModel

/**
 * Single activity hosting the native (Compose) control panel.
 *
 * The Python gateway runs in [GatewayService]; this screen only talks to it
 * over loopback HTTP, so the UI stays responsive while the gateway boots and
 * survives configuration changes.
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        // Battery optimisation is what kills the gateway a few minutes after the
        // screen turns off on most Chinese ROMs.  Ask once, and only if the
        // system actually lets us (some ROMs route this to a settings page).
        if (Build.VERSION.SDK_INT >= 23) {
            val pm = getSystemService(android.os.PowerManager::class.java)
            if (pm != null && !pm.isIgnoringBatteryOptimizations(packageName)) {
                runCatching {
                    startActivity(Intent(
                        Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        Uri.parse("package:$packageName")))
                }.onFailure {
                    runCatching {
                        startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
                    }
                }
            }
        }

        AssetBridge.appContext = applicationContext
        GatewayService.start(this)

        setContent {
            val vm: GatewayViewModel = viewModel()
            GatewayApp(vm)
        }
    }
}
