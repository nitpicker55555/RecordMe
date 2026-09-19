#!/bin/bash
# 编定位助手。改了 location_helper.swift 之后跑一次。
set -e
cd "$(dirname "$0")"
swiftc -O -o LocationHelper.app/Contents/MacOS/location_helper location_helper.swift
codesign -s - -f LocationHelper.app
echo "→ $(pwd)/LocationHelper.app"
