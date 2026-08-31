#!/usr/bin/env bash
# Phase 1 PoC: register 9901 on the PBX, dial a paging extension, play a WAV, hang up.
#   ./phase1/page.sh                       -> register only, no call
#   ./phase1/page.sh 991                   -> page 991 with announce.wav
#   ./phase1/page.sh 991 chime_announce    -> page 991 with that clip
# Env knobs: LEADIN (s before playback starts, default 1), MAXCALL (hangup guard, default 25)
set -euo pipefail
cd "$(dirname "$0")/.."

TARGET="${1:-}"
CLIP="${2:-announce}"
LEADIN="${LEADIN:-1}"
MAXCALL="${MAXCALL:-25}"

WAV="$PWD/phase1/sounds/${CLIP}.wav"
[[ -f "$WAV" ]] || { echo "no such clip: $WAV" >&2; exit 1; }

set -a; . ./.env; set +a
CFG="$PWD/phase1/baresip"
umask 077
cat > "$CFG/accounts" <<EOF
<sip:${SIP_EXTENSION}@${PBX_HOST}:${PBX_PORT}>;auth_user=${SIP_EXTENSION};auth_pass=${SIP_SECRET};transport=udp;regint=300;answermode=manual;audio_codecs=PCMU,PCMA;ptime=20;medianat=;rtcp_mux=no
EOF

# aufile is the audio *source*: whatever WAV it points at is what the far end hears.
sed -i '' -E "s|^audio_source .*|audio_source            aufile,${WAV}|" "$CFG/config"

DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$WAV")
echo "clip=$WAV duration=${DUR}s lead-in=${LEADIN}s target=${TARGET:-<none, register only>}"

{
  sleep 4                                  # let REGISTER complete
  if [[ -n "$TARGET" ]]; then
    echo "/dial $TARGET"
    # hang up on clip duration + lead-in + slack, or MAXCALL, whichever is smaller
    HANG=$(python3 -c "print(min($MAXCALL, $DUR + $LEADIN + 2))")
    sleep "$HANG"
    echo "/hangup"
    sleep 1
  else
    sleep 6
  fi
  echo "/quit"
} | baresip -f "$CFG" -v 2>&1
