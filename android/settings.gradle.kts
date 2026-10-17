pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
        maven("https://chaquo.com/maven")   // Chaquopy gradle plugin
    }
}
dependencyResolutionManagement {
    repositoriesMode = RepositoriesMode.FAIL_ON_PROJECT_REPOS
    repositories {
        google()
        mavenCentral()
        maven("https://chaquo.com/maven")   // Chaquopy pip wheels
    }
}
rootProject.name = "loomy2api"
include(":app")
