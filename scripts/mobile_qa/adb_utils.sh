#!/bin/bash
ADB_HOST="${ADB_HOST:-localhost}"
ADB_PORT="${ADB_PORT:-5555}"

connect() { adb connect $ADB_HOST:$ADB_PORT; }
install_apk() { adb -s $ADB_HOST:$ADB_PORT install -r "$1"; }
screenshot() {
  adb -s $ADB_HOST:$ADB_PORT exec-out screencap -p > "$1"
  echo "Screenshot saved: $1"
}
tap() { adb -s $ADB_HOST:$ADB_PORT shell input tap $1 $2; }
swipe() { adb -s $ADB_HOST:$ADB_PORT shell input swipe $1 $2 $3 $4; }
text_input() { adb -s $ADB_HOST:$ADB_PORT shell input text "$1"; }
launch_app() { adb -s $ADB_HOST:$ADB_PORT shell am start -n "$1"; }
kill_app() { adb -s $ADB_HOST:$ADB_PORT shell am force-stop "$1"; }
get_logs() { adb -s $ADB_HOST:$ADB_PORT logcat -d -t "$1" > "$2"; }
is_running() { adb -s $ADB_HOST:$ADB_PORT shell pidof "$1"; }
