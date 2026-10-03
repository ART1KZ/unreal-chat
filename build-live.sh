#!/usr/bin/env bash
# Build the small pinned harness adapter. Go is build-time only, never runtime.
set -euo pipefail
repo=$(cd "$(dirname "$0")" && pwd)
target="${UACHAT_LIVE_BINARY:-$HOME/.local/bin/uachat-live-runner}"
cache="${XDG_CACHE_HOME:-$HOME/.cache}/uachat-build"
if [ "${1:-}" = --check ]; then
  [ -x "$target" ] && "$target" --version
  exit $?
fi
mkdir -p "$cache" "$(dirname "$target")"
go_cmd=$(command -v go || true)
if [ -z "$go_cmd" ] && [ -x "$cache/go/bin/go" ]; then go_cmd="$cache/go/bin/go"; fi
if [ -z "$go_cmd" ]; then
  echo 'live: fetching verified Go 1.27.1 toolchain (build-time only)'
  CACHE="$cache" python3 - <<'PY'
import hashlib,json,os,platform,urllib.request
version='go1.27.1'
arch={'x86_64':'amd64','aarch64':'arm64','arm64':'arm64'}.get(platform.machine())
system={'Linux':'linux','Darwin':'darwin'}.get(platform.system())
if not arch or not system: raise SystemExit('live build: unsupported platform')
name=f'{version}.{system}-{arch}.tar.gz'
with urllib.request.urlopen('https://go.dev/dl/?mode=json&include=all',timeout=30) as r: data=json.load(r)
release=next(item for item in data if item['version']==version)
asset=next(item for item in release['files'] if item['filename']==name)
path=os.path.join(os.environ['CACHE'],'go.tar.gz')
with urllib.request.urlopen('https://go.dev/dl/'+name,timeout=120) as response,open(path+'.download','wb') as stream:
    digest=hashlib.sha256()
    while chunk:=response.read(1024*1024): digest.update(chunk);stream.write(chunk)
if digest.hexdigest()!=asset['sha256']:
    os.unlink(path+'.download');raise SystemExit('Go SHA256 mismatch')
os.replace(path+'.download',path)
PY
  tar -xzf "$cache/go.tar.gz" -C "$cache"
  go_cmd="$cache/go/bin/go"
fi
export PATH="$(dirname "$go_cmd"):$PATH"
echo 'live: building against unreal-agent v0.2.0'
cd "$repo/live-runner"
# go.sum pins transitive dependencies; do not silently upgrade the harness.
"$go_cmd" test ./...
"$go_cmd" build -mod=readonly -trimpath -o "$target.new" .
chmod 755 "$target.new"
"$target.new" --version
mv -f "$target.new" "$target"
echo "live: installed $target"
