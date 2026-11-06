package com.loomy.gw

import android.app.Activity
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.webkit.WebView
import android.webkit.WebViewClient
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/**
 * Shows the gateway's own /panel page in a WebView.
 *
 * The foreground service starts Python asynchronously, so the first load can
 * land before the gateway is listening.  Instead of showing the WebView's
 * "connection refused" page (which never retries on its own), wait until
 * /health answers and only then load the panel.
 */
class PanelActivity : Activity() {

    private lateinit var webView: WebView
    private val ui = Handler(Looper.getMainLooper())
    private val probe = Executors.newSingleThreadExecutor()
    @Volatile private var destroyed = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WebView.setWebContentsDebuggingEnabled(true)   // chrome://inspect diagnostics
        webView = WebView(this)
        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.webViewClient = WebViewClient()
        webView.webChromeClient = object : android.webkit.WebChromeClient() {
            override fun onConsoleMessage(msg: android.webkit.ConsoleMessage): Boolean {
                android.util.Log.w("PanelWebView", "${msg.message()} @${msg.lineNumber()}")
                return true
            }
        }
        setContentView(webView)
        waitForGatewayThenLoad()
    }

    /** Poll /health until the Python gateway accepts connections, then load. */
    private fun waitForGatewayThenLoad(attempt: Int = 0) {
        if (destroyed) return
        probe.execute {
            val ready = try {
                val conn = URL("$BASE_URL/health").openConnection() as HttpURLConnection
                conn.connectTimeout = 1000
                conn.readTimeout = 1000
                val code = conn.responseCode
                conn.disconnect()
                code == 200
            } catch (_: Exception) {
                false
            }
            ui.post {
                if (destroyed) return@post
                if (ready) {
                    webView.loadUrl("$BASE_URL/panel")
                } else if (attempt < MAX_ATTEMPTS) {
                    // gateway still booting (unpack + import + model fetch)
                    ui.postDelayed({ waitForGatewayThenLoad(attempt + 1) }, RETRY_MS)
                } else {
                    webView.loadUrl("$BASE_URL/panel")   // show the real error
                }
            }
        }
    }

    override fun onBackPressed() {
        // back = home; the service keeps running
        moveTaskToBack(true)
    }

    override fun onDestroy() {
        destroyed = true
        probe.shutdownNow()
        ui.removeCallbacksAndMessages(null)
        webView.destroy()
        super.onDestroy()
    }

    companion object {
        private val BASE_URL: String
            get() = "http://127.0.0.1:${GatewayService.PORT}"
        private const val RETRY_MS = 250L
        private const val MAX_ATTEMPTS = 120      // ~30 s, generous for a cold start
    }
}
