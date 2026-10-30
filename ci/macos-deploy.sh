#!/usr/bin/env bash

set -ex

python3 ci/macos_app_cleaner.py
python3 ci/macos_app_arch_check.py

mkdir dist/Syncplay.app/Contents/Resources/English.lproj
mkdir dist/Syncplay.app/Contents/Resources/en_AU.lproj
mkdir dist/Syncplay.app/Contents/Resources/en_GB.lproj
mkdir dist/Syncplay.app/Contents/Resources/German.lproj
mkdir dist/Syncplay.app/Contents/Resources/Italian.lproj
mkdir dist/Syncplay.app/Contents/Resources/ru.lproj
mkdir dist/Syncplay.app/Contents/Resources/Spanish.lproj
mkdir dist/Syncplay.app/Contents/Resources/es_419.lproj

python3 ci/macos_app_adhoc_sign.py

pip3 install dmgbuild
mv syncplay/resources/macOS_readme.pdf syncplay/resources/.macOS_readme.pdf

# hdiutil detach can fail transiently on CI runners ("Resource busy", a known
# dmgbuild issue: https://github.com/dmgbuild/dmgbuild/issues/26). The CLI
# defaults to only 5 detach attempts; raise them and retry the whole build
# once after force-detaching any leftover volume.
DMG="dist_actions/Syncplay_${VER}.dmg"
build_dmg() {
    python3 -m dmgbuild --detach-retries 8 -s appdmg.py "Syncplay" "$DMG"
}

if ! build_dmg; then
    echo "dmgbuild failed, cleaning up and retrying..."
    rm -f "$DMG"
    hdiutil detach -force "/Volumes/Syncplay" >/dev/null 2>&1 || true
    sleep 5
    build_dmg
fi
