#!/usr/bin/env bash
# Build a .deb package for micro-agent.
#
# Layout produced by this script (Debian policy compliant):
#   usr/bin/micro-agent                         # thin wrapper
#   usr/share/micro-agent/{micro.py,...}        # app code
#   usr/share/micro-agent/.env.example          # template config
#   usr/share/doc/micro-agent/{README,README.md,changelog.gz}
#   DEBIAN/{control,postinst,md5sums}
#
# Usage:  ./build-deb.sh [version]   (default version: 0.1.0)
set -euo pipefail

# ── Config ──────────────────────────────────────────────────────────────
PKG="micro-agent"
VERSION="${1:-0.1.0}"
ARCH="all"
MAINTAINER="adsoftware <adsoftware@users.noreply.github.com>"
DESC="Tiny terminal AI agent that drives a real Chromium browser over CDP."
LONG_DESC="$(printf ' Micro is a compact, terminal-based AI agent (~620 lines) that calls LLM\n providers (GLM 5.2 / DeepSeek / OpenRouter / OpenCode / Ollama) and drives a\n real Chromium browser over the Chrome DevTools Protocol. It ships tools for\n shell execution, vision (screenshot analysis), DuckDuckGo search, and\n JS-rendered webpage fetch.\n .\n Provider and model can be selected via the PROVIDER constant or CLI flags\n (-zai, -ollama, etc.).')"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

WORK="$(mktemp -d)"
STAGE="$WORK/${PKG}_${VERSION}_${ARCH}"
trap 'rm -rf "$WORK"' EXIT

APPDIR="$STAGE/usr/share/${PKG}"
DOCDIR="$STAGE/usr/share/doc/${PKG}"
BINDIR="$STAGE/usr/bin"
DEBDIR="$STAGE/DEBIAN"

mkdir -p "$APPDIR" "$DOCDIR" "$BINDIR" "$DEBDIR"

# ── Application files ───────────────────────────────────────────────────
install -m 0644 micro.py          "$APPDIR/micro.py"
install -m 0644 cdp_fetch.py      "$APPDIR/cdp_fetch.py"
install -m 0644 browser_action.py "$APPDIR/browser_action.py"
install -m 0644 .env.example      "$APPDIR/.env.example"

# ── Docs ────────────────────────────────────────────────────────────────
install -m 0644 README.md         "$DOCDIR/README.md"
install -m 0644 LICENSE           "$DOCDIR/copyright"
# Debian changelog (gzipped) — minimal single entry
cat > "$DOCDIR/changelog" <<EOF
${PKG} (${VERSION}) unstable; urgency=medium

  * Initial Debian package.

 -- ${MAINTAINER}  $(date -R)
EOF
gzip -9nf "$DOCDIR/changelog"
# Some tooling also expects a copyright symlink-free real file is fine above.

# ── Wrapper script (/usr/bin/micro-agent) ───────────────────────────────
cat > "$BINDIR/${PKG}" <<'EOF'
#!/bin/sh
# Wrapper for micro-agent. Runs from /usr/share/micro-agent so its sibling
# modules (cdp_fetch, browser_action) are importable.
MICRO_HOME="/usr/share/micro-agent"
cd "$MICRO_HOME" 2>/dev/null || true
exec python3 "$MICRO_HOME/micro.py" "$@"
EOF
chmod 0755 "$BINDIR/${PKG}"

# ── postinst: optional pip dep hint ─────────────────────────────────────
cat > "$DEBDIR/postinst" <<'EOF'
#!/bin/sh
set -e
# Soft-dependency note: the web-search tool needs `ddgs`, which is not in
# Debian apt. If the user wants DuckDuckGo search, they should run:
#   pip3 install --break-system-packages ddgs
# All other features work without it.
if [ "$1" = "configure" ]; then
    echo ""
    echo "micro-agent installed. Set up your API keys:"
    echo "  mkdir -p ~/.config/micro-agent"
    echo "  cp /usr/share/micro-agent/.env.example ~/.config/micro-agent/.env"
    echo "  nano ~/.config/micro-agent/.env"
    echo "Then run:  micro-agent"
    echo ""
    echo "To change default URLs/models, set *_URL / *_MODEL vars in .env"
    echo "(e.g. OPENROUTER_MODEL=anthropic/claude-3.5-sonnet)."
    echo ""
    echo "Chat logs are written to ~/.config/micro-agent/chat.*.log.txt"
    echo ""
fi
exit 0
EOF
chmod 0755 "$DEBDIR/postinst"

# ── DEBIAN/control ──────────────────────────────────────────────────────
INSTALLED_SIZE="$(du -sk "$STAGE/usr" | cut -f1)"
cat > "$DEBDIR/control" <<EOF
Package: ${PKG}
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${ARCH}
Installed-Size: ${INSTALLED_SIZE}
Maintainer: ${MAINTAINER}
Depends: python3 (>= 3.10), python3-requests, python3-pil, python3-dotenv
Recommends: chromium | chromium-browser | google-chrome
Suggests: gnome-screenshot | scrot, python3-pip
Description: ${DESC}
${LONG_DESC}
EOF

# ── md5sums ─────────────────────────────────────────────────────────────
# md5sums should list files relative to the package root with a leading /
( cd "$STAGE" && find . -type f ! -path "./DEBIAN/*" -printf '%P\n' \
    | while read -r f; do
        md5sum "./$f" | sed "s| \./| /|"
      done ) > "$DEBDIR/md5sums"

# ── Build ───────────────────────────────────────────────────────────────
# --root-owner-group makes every entry owned by root:root inside the deb
# without needing fakeroot introspection.
echo "Building ${PKG}_${VERSION}_${ARCH}.deb ..."
dpkg-deb --root-owner-group --build "$STAGE" "$ROOT/${PKG}_${VERSION}_${ARCH}.deb"

echo "OK: $ROOT/${PKG}_${VERSION}_${ARCH}.deb"
dpkg-deb --info "$ROOT/${PKG}_${VERSION}_${ARCH}.deb" | sed -n '1,30p'
echo "---"
echo "Contents:"
dpkg-deb -c "$ROOT/${PKG}_${VERSION}_${ARCH}.deb" | sed -n '1,40p'
