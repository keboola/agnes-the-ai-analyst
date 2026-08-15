#!/bin/zsh
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
client_dir="$(cd "$script_dir/.." && pwd)"
repo_dir="$(cd "$client_dir/../.." && pwd)"
bundle_dir="$client_dir/dist/Agnes.app"

rm -rf "$bundle_dir"
mkdir -p "$bundle_dir/Contents/MacOS" "$bundle_dir/Contents/Resources"

swift build --configuration release --product AgnesDesktop --package-path "$client_dir"
bin_dir="$(swift build --configuration release --package-path "$client_dir" --show-bin-path)"
cp "$bin_dir/AgnesDesktop" "$bundle_dir/Contents/MacOS/AgnesDesktop"
cp -R "$bin_dir/AgnesDesktop_AgnesDesktop.bundle" "$bundle_dir/Contents/Resources/"
cp "$client_dir/App/Info.plist" "$bundle_dir/Contents/Info.plist"
cp "$repo_dir/LICENSE" "$bundle_dir/Contents/Resources/LICENSE"

echo "Built unsigned app: $bundle_dir"
