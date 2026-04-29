#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ -x ./gradlew ]]; then
  GRADLE_CMD=(./gradlew)
elif command -v gradle >/dev/null 2>&1; then
  GRADLE_CMD=(gradle)
elif command -v docker >/dev/null 2>&1 && [[ "${AADS_ANDROID_DOCKER_FALLBACK:-1}" == "1" ]]; then
  docker run --rm \
    -v "$ROOT_DIR:/workspace" \
    -w /workspace \
    ghcr.io/cirruslabs/android-sdk:35 \
    bash -lc 'set -euo pipefail
      if ! command -v unzip >/dev/null 2>&1; then
        apt-get update && apt-get install -y unzip curl
      fi
      curl -fsSL https://services.gradle.org/distributions/gradle-8.10.2-bin.zip -o /tmp/gradle.zip
      unzip -q /tmp/gradle.zip -d /opt
      /opt/gradle-8.10.2/bin/gradle :app:assembleDebug
      mkdir -p dist
      cp app/build/outputs/apk/debug/app-debug.apk dist/aads-agent-debug.apk
      ls -lh dist/aads-agent-debug.apk'
  exit $?
else
  echo "Gradle is not installed and ./gradlew is not present." >&2
  echo "Install Gradle/Android SDK, add a Gradle wrapper, or enable Docker fallback." >&2
  exit 127
fi

"${GRADLE_CMD[@]}" :app:assembleDebug

mkdir -p dist
cp app/build/outputs/apk/debug/app-debug.apk dist/aads-agent-debug.apk
echo "Built: $ROOT_DIR/dist/aads-agent-debug.apk"
