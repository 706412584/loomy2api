# keep manifest components: R8 renaming breaks startActivity / startService lookups
-keep public class * extends android.app.Activity
-keep public class * extends android.app.Service
-keep public class * extends android.content.BroadcastReceiver

# keep the crash-relevant defaults
-keepattributes SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile

# AssetBridge is looked up reflectively from Python via jclass()
-keep class com.loomy.gw.AssetBridge { *; }
