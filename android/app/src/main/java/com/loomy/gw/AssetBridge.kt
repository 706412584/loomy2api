package com.loomy.gw

import android.content.Context

/** Tiny Java-side helper so Python code can reach the app Context's assets. */
object AssetBridge {
    @JvmStatic
    fun assets(): android.content.res.AssetManager =
        appContext!!.assets

    @Volatile
    var appContext: Context? = null
}
