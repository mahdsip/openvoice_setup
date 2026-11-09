#!/usr/bin/env bash
# Re-apply the customisations in this repo on top of a fresh OVOS install.
#
# Run on the Pi, as the OVOS user (OVOS_USER in setup.env), from the repo root, AFTER the
# official ovos-installer has finished:
#
#   cp setup.env.example setup.env && $EDITOR setup.env
#   ./install/apply.sh            # everything
#   ./install/apply.sh ovos hivemind   # only some sections
#
# Sections: system user systemd ovos hivemind proxy openwebui
# Existing files are backed up as <file>.bak-<timestamp> before being replaced.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TS="$(date +%Y%m%d%H%M%S)"
[ -f "$REPO/setup.env" ] || { echo "missing setup.env (copy setup.env.example)"; exit 1; }
set -a; . "$REPO/setup.env"; set +a
OVOS_USER="${OVOS_USER:?set OVOS_USER in setup.env}"
[ "$(id -un)" = "$OVOS_USER" ] || { echo "run as ${OVOS_USER}"; exit 1; }
HOME_DIR="/home/${OVOS_USER}"
OVOS_UID="$(id -u)"; OVOS_GID="$(id -g)"
UV="${HOME_DIR}/.local/bin/uv"
BIND_IP="${BIND_IP:-127.0.0.1}"
SUBST_VARS='${HA_TOKEN} ${HA_URL} ${PLEX_TOKEN} ${PLEX_URL} ${FAMILIA_PROXY_API_KEY} ${OPENWEBUI_SECRET_KEY}'
# __TOKENS__ in the repo files: host-specific values kept out of the public repo
tokens() { sed -e "s/__OVOS_USER__/${OVOS_USER}/g" -e "s/__OVOS_UID__/${OVOS_UID}/g" \
               -e "s/__OVOS_GID__/${OVOS_GID}/g" -e "s/__BIND_IP__/${BIND_IP}/g"; }

log() { printf '\n== %s\n' "$*"; }
backup() { [ -e "$1" ] && cp -a "$1" "$1.bak-$TS" || true; }
sbackup() { sudo test -e "$1" && sudo cp -a "$1" "$1.bak-$TS" || true; }

# install a user file, substituting only our placeholders and the bind IP
put_user() {  # src dst [mode]
  mkdir -p "$(dirname "$2")"; backup "$2"
  envsubst "$SUBST_VARS" < "$1" | tokens > "$2"
  chmod "${3:-644}" "$2"
}
put_root() {  # src dst [mode]
  sudo mkdir -p "$(dirname "$2")"; sbackup "$2"
  tokens < "$1" | sudo tee "$2" >/dev/null
  sudo chmod "${3:-644}" "$2"
}
need_secret() { [ -n "${!1:-}" ] || { echo "setup.env: $1 is empty"; exit 1; }; }

site() { echo "$1"/lib/python3*/site-packages; }

do_system() {
  log "system tuning (sysctl, udev, modprobe, zram/swap, tmpfs)"
  for f in sysctl.d/98-zram.conf sysctl.d/99-ovos.conf udev/rules.d/60-scheduler.rules \
           modprobe.d/snd-usb-audio.conf rpi/swap.conf; do
    put_root "$REPO/system/etc/$f" "/etc/$f"
  done
  sbackup /etc/fstab
  while IFS= read -r line; do
    mnt="$(awk '{print $2}' <<<"$line")"
    grep -qsE "^[^#]+[[:space:]]${mnt}[[:space:]]" /etc/fstab || echo "$line" | sudo tee -a /etc/fstab >/dev/null
  done < "$REPO/system/etc/fstab.ovos-lines"
  mkdir -p "${HOME_DIR}/.local/state/mycroft" "${HOME_DIR}/.local/state/mycroft-persistent"
  sudo sysctl --system >/dev/null; sudo udevadm control --reload
}

do_user() {
  log "user config (pipewire, wireplumber, environment.d, labwc, kanshi)"
  (cd "$REPO/user-config" && find . -type f) | while read -r f; do
    put_user "$REPO/user-config/$f" "${HOME_DIR}/.config/${f#./}"
  done
  for f in wireplumber.service.d/98-performance.conf wireplumber.service.d/99-noswap.conf; do
    put_user "$REPO/systemd/user/$f" "${HOME_DIR}/.config/systemd/user/$f"
  done
}

do_systemd() {
  log "systemd units and drop-ins"
  (cd "$REPO/systemd/system" && find . -type f) | while read -r f; do
    put_root "$REPO/systemd/system/$f" "/etc/systemd/system/${f#./}"
  done
  sudo systemctl daemon-reload
  sudo systemctl enable ovos.service ovos-messagebus.service ovos-core.service ovos-audio.service \
    ovos-listener.service ovos-phal.service ovos-phal-admin.service \
    ovos-mycroft-state-restore.service ovos-mycroft-state-sync.timer \
    hivemind-core.service hivemind-webchat.service familia-ovos-proxy.service open-webui.service
}

do_ovos() {
  need_secret HA_TOKEN; need_secret HA_URL; need_secret PLEX_TOKEN; need_secret PLEX_URL; need_secret FAMILIA_PROXY_API_KEY
  local V="${HOME_DIR}/.venvs/ovos"
  [ -x "$V/bin/python" ] || { echo "no OVOS venv: run ovos-installer first"; exit 1; }
  log "OVOS: plex skill"
  "$UV" pip install --python "$V/bin/python" "$REPO/skills/ovos-skill-plex"
  log "OVOS: Spanish OCP play.intent (more 'pon/ponme ...' phrases)"
  local SP; SP="$(site "$V")"
  local OCPV; OCPV="$("$V/bin/python" -c 'import importlib.metadata as m;print(m.version("ovos-ocp-pipeline-plugin"))')"
  [ "$OCPV" = "1.1.18" ] || echo "WARNING: ovos-ocp-pipeline-plugin is $OCPV, patch made for 1.1.18; check patches/"
  backup "$SP/ocp_pipeline/locale/es-es/play.intent"
  patch -N -s "$SP/ocp_pipeline/locale/es-es/play.intent" \
    < "$REPO/patches/ovos-ocp-pipeline-plugin-1.1.18-es-play-intent.diff" || echo "(already patched?)"
  log "OVOS: Vosk models (STT + 'hey tiki' wake word) and listener sounds"
  local VOSK="${HOME_DIR}/.local/share/vosk"
  mkdir -p "$VOSK"
  for m in vosk-model-small-es-0.3 vosk-model-small-en-us-0.15; do
    if [ ! -d "$VOSK/$m" ]; then
      curl -fsSL -o "/tmp/$m.zip" "https://alphacephei.com/vosk/models/$m.zip"
      unzip -q "/tmp/$m.zip" -d "$VOSK" && rm "/tmp/$m.zip"
    fi
  done
  # "sound": "snd/start_listening.wav" in mycroft.conf resolves against ~/.mycroft
  mkdir -p "${HOME_DIR}/.mycroft/snd"
  cp -n "$SP"/ovos_dinkum_listener/res/snd/{acknowledge.mp3,error.mp3,start_listening.wav} "${HOME_DIR}/.mycroft/snd/"
  log "OVOS: configuration"
  (cd "$REPO/ovos/config" && find . -type f) | while read -r f; do
    case "$f" in
      ./OpenVoiceOS/*) put_user "$REPO/ovos/config/$f" "${HOME_DIR}/.config/${f#./}" ;;
      ./ovos_persona/*) put_user "$REPO/ovos/config/$f" "${HOME_DIR}/.config/${f#./}" 600 ;;
      ./skills/*/settings.json|./mycroft.conf) put_user "$REPO/ovos/config/$f" "${HOME_DIR}/.config/mycroft/${f#./}" 600 ;;
      *) put_user "$REPO/ovos/config/$f" "${HOME_DIR}/.config/mycroft/${f#./}" ;;
    esac
  done
}

do_hivemind() {
  local V="${HOME_DIR}/.venvs/hivemind"
  log "HiveMind: venv (system python3.13)"
  [ -x "$V/bin/python" ] || python3 -m venv "$V"
  "$V/bin/pip" install -q -r "$REPO/requirements/hivemind.txt"
  log "HiveMind: core config"
  put_user "$REPO/hivemind/config/server.json" "${HOME_DIR}/.config/hivemind-core/server.json"
  log "HiveMind: webchat (vendored JS with HELLO/session/UTF-8 fixes, bind wrapper)"
  local W; W="$(site "$V")/hivemind_webchat"
  mkdir -p "$W/static/js/vendor"
  for f in asmcrypto.js webcrypto-shim.js hivemind.js; do
    put_user "$REPO/hivemind/webchat/hivemind_webchat/static/js/vendor/$f" "$W/static/js/vendor/$f"
  done
  put_user "$REPO/hivemind/webchat/hivemind_webchat/templates/index.html" "$W/templates/index.html"
  put_user "$REPO/hivemind/webchat/hivemind-webchat-local" "${HOME_DIR}/.local/bin/hivemind-webchat-local" 755
  if ! "$V/bin/hivemind-core" list-clients 2>/dev/null | grep -q webchat-casa; then
    echo "NOTE: create the webchat client: $V/bin/hivemind-core add-client --name webchat-casa"
  fi
}

do_proxy() {
  log "familIA proxy in /opt/familIA"
  sudo mkdir -p /opt/familIA /etc/familIA
  [ -x /opt/familIA/venv/bin/python ] || sudo python3 -m venv /opt/familIA/venv
  sudo /opt/familIA/venv/bin/pip install -q -r "$REPO/requirements/familia-proxy.txt"
  for f in ovos_openai_proxy.py familia_mcp_bridge.py; do put_root "$REPO/familia-proxy/$f" "/opt/familIA/$f"; done
  if ! sudo test -f /etc/familIA/proxy.env; then
    sudo install -m 600 -o root -g root "$REPO/familia-proxy/proxy.env.example" /etc/familIA/proxy.env
    echo "NOTE: fill in /etc/familIA/proxy.env (sudoedit), then: sudo systemctl restart familia-ovos-proxy"
  fi
}

do_openwebui() {
  need_secret OPENWEBUI_SECRET_KEY; need_secret FAMILIA_PROXY_API_KEY
  local V="${HOME_DIR}/.venvs/openwebui"
  log "Open WebUI: venv (uv python 3.11) - large download, needs ~3 GB free"
  [ -x "$V/bin/python" ] || "$UV" venv --python 3.11 "$V"
  "$UV" pip install --python "$V/bin/python" -r "$REPO/requirements/openwebui.txt"
  put_user "$REPO/open-webui/env.example" "${HOME_DIR}/.config/open-webui/env" 600
}

SECTIONS=("$@"); [ ${#SECTIONS[@]} -gt 0 ] || SECTIONS=(system user systemd ovos hivemind proxy openwebui)
for s in "${SECTIONS[@]}"; do "do_$s"; done

log "done. Restart with: sudo systemctl restart ovos hivemind-core hivemind-webchat familia-ovos-proxy open-webui"
echo "Listening on BIND_IP=${BIND_IP} for HiveMind (5678), webchat (9090), Open WebUI (3000)."
