#!/usr/bin/env bash
# Bring up a Cloudflare WARP egress as a userspace SOCKS5 proxy (no root, no
# NET_ADMIN, no kernel WireGuard interface) for the caption LIST call.
#
# WHY: YouTube IP-blocks datacenter egress (Render/AWS/GCP) for the innertube
# caption listing — measured 0/22 hard videos direct from Render. WARP egresses
# from Cloudflare's AS13335 ranges, which YouTube does NOT block — measured
# 22/22 through this exact setup. wgcf generates a free WARP WireGuard config;
# wireproxy runs it in userspace and exposes SOCKS5 on 127.0.0.1:$WARP_SOCKS_PORT.
#
# Point the app at it with:  CAPTION_PROXY_URL=socks5h://127.0.0.1:25344
#
# REQUIREMENT: the host must allow OUTBOUND UDP to engage.cloudflareclient.com
# (ports 2408, with fallbacks 500/1701/4500). WireGuard is UDP-only. If your PaaS
# blocks outbound UDP, use the Cloudflare Worker relay instead (see
# docs/caption-egress.md) — it needs no UDP.
set -euo pipefail

WARP_DIR="${WARP_DIR:-/tmp/warp}"
WARP_SOCKS_PORT="${WARP_SOCKS_PORT:-25344}"
WGCF_VERSION="${WGCF_VERSION:-2.2.31}"
WIREPROXY_VERSION="${WIREPROXY_VERSION:-1.1.2}"

mkdir -p "$WARP_DIR"
cd "$WARP_DIR"

arch="$(uname -m)"; os="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$arch" in
  x86_64|amd64) wgcf_arch="amd64"; wp_arch="amd64" ;;
  aarch64|arm64) wgcf_arch="arm64"; wp_arch="arm64" ;;
  *) echo "unsupported arch $arch" >&2; exit 1 ;;
esac

# Fetch wgcf + wireproxy once (cached in $WARP_DIR across restarts if persistent).
if [ ! -x "./wgcf" ]; then
  curl -fsSL -o wgcf "https://github.com/ViRb3/wgcf/releases/download/v${WGCF_VERSION}/wgcf_${WGCF_VERSION}_${os}_${wgcf_arch}"
  chmod +x wgcf
fi
if [ ! -x "./wireproxy" ]; then
  curl -fsSL -o wireproxy.tgz "https://github.com/whyvl/wireproxy/releases/download/v${WIREPROXY_VERSION}/wireproxy_${os}_${wp_arch}.tar.gz"
  tar xzf wireproxy.tgz wireproxy && chmod +x wireproxy && rm -f wireproxy.tgz
fi

export WGCF_ACCEPT_TOS=yes
# Register + generate a fresh WARP identity if we don't have one yet. Registration
# is Cloudflare-rate-limited; retry with backoff.
if [ ! -f wgcf-account.toml ]; then
  for i in 1 2 3 4 5; do
    ./wgcf register --accept-tos && break || { echo "register retry $i"; sleep $((8 + i * 5)); }
  done
fi
./wgcf generate

priv="$(grep PrivateKey wgcf-profile.conf | awk '{print $3}')"
pub="$(grep PublicKey  wgcf-profile.conf | awk '{print $3}')"
endpoint_ip="$(getent hosts engage.cloudflareclient.com 2>/dev/null | awk '{print $1}' | head -1)"
[ -z "${endpoint_ip:-}" ] && endpoint_ip="162.159.192.1"

cat > wireproxy.conf <<EOF
[Interface]
PrivateKey = ${priv}
Address = 172.16.0.2/32
DNS = 1.1.1.1
MTU = 1280

[Peer]
PublicKey = ${pub}
Endpoint = ${endpoint_ip}:2408
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25

[Socks5]
BindAddress = 127.0.0.1:${WARP_SOCKS_PORT}
EOF

echo "Starting wireproxy SOCKS5 on 127.0.0.1:${WARP_SOCKS_PORT} (WARP egress)"
exec ./wireproxy -c wireproxy.conf
