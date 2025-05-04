#!/usr/bin/env bash
set -euo pipefail

# ─── 1. Подготовка окружения ────────────────────────────────────────────────────
source /opt/vnc-venv/bin/activate          # vncdotool

# ─── 2. Карта «VNC‑адрес → параметры восстановления» ────────────────────────────
# Формат значения:  VMID|STORAGE_ID|BACKUP_DIR
#   • VMID        — ID виртуальной машины
#   • STORAGE_ID  — куда разворачивать бэкап (ID хранилища из /etc/pve/storage.cfg)
#   • BACKUP_DIR  — каталог, где лежат файлы vzdump‑qemu‑<VMID>-*.vma(.zst)
#
# Если хотите жёстко задать конкретный файл, вместо каталога укажите
# полный путь к .vma(.zst) — тогда поиск не выполняется.
                 # стандартная длительность, сек
EXTRA_TIME="${1:-0}" 
SHARE="//192.168.100.10/AVLogs"  # куда грузим .mp4
OUTDIR="/root"                  # где создавать файлы

if ! [[ "$EXTRA_TIME" =~ ^[0-9]+$ ]]; then
  echo "❌ Неверный аргумент: должен быть числом"
  exit 1
fi

declare -A VM_MAP=(
  ["127.0.0.1:77"]="101|avast"
  ["127.0.0.1:78"]="102|avira"
#  ["127.0.0.1:79"]="103|noAV"
#  ["127.0.0.1:80"]="104"
#  ["127.0.0.1:81"]="105"
)

VMS=("$@")
TOTAL="${#VM_MAP[@]}"

curl -sf -X POST "http://127.0.0.1:5000/start/${TOTAL}"


# ─── 3. Функции ────────────────────────────────────────────────────────────────

vnc_actions() {
  local server="$1"
  echo "[${server}] ▶  VNC actions"
  vncdotool -s "$server" \
    move 720 680 sleep 0.1 click 1 sleep 0.3 click 1 sleep 8 \
    move 765 185 sleep 0.1 mousedown 1 sleep 0.1 drag 820 185 sleep 0.1 mouseup 1 sleep 0.1 \
    move 675 401 sleep 0.1 click 1 sleep 15 \
    move 804 188 sleep 0.1 click 1 sleep 0.3 click 1 \
    move 675 401 sleep 0.1 click 1 sleep 5
}

shutdown_vm() {
  local vmid="$1"
  echo "[VM $vmid] ▶  Shutdown"
  if ! qm shutdown "$vmid" --timeout 120 --skiplock; then
    echo "[VM $vmid] ⏱  Timeout — принудительный stop"
    qm stop "$vmid" --skiplock
  fi
}

restore_latest_local() {
  local vmid="$1"

  # берём самый «свежий» архив по алфавиту (дата есть в имени)
  local volid
  volid=$(pvesm list local --content backup --vmid "$vmid" \
           | awk 'NR>1 {print $1}' | sort | tail -1)

  if [[ -z "$volid" ]]; then
    echo "[VM $vmid] ❌ Back‑up not found on 'local'"
    return 1
  fi

  echo "[VM $vmid] ▶ restore ← ${volid##*/}"
  qm unlock "$vmid" 2>/dev/null || true
  qmrestore "$volid" "$vmid" --storage local --force
}

start_vm() {
  local vmid="$1"
  echo "[VM $vmid] ▶ start"
  qm start "$vmid"
}

# Главная обёртка
vnc_shutdown_restore() {
  local server="$1"
  IFS='|' read -r vmid antivirus <<<"${VM_MAP[$server]}"

  # ---------- 1. старт записи --------------------------------------
  local stamp flv mp4 rec_pid
  stamp=$(date +%F_%H-%M-%S)
  flv="${OUTDIR}/${antivirus}_${stamp}.flv"
  mp4="${OUTDIR}/${antivirus}.mp4"

  echo "[${server}] ▶ REC start → ${flv}"
  flvrec.py -o "$flv" -d "$server" &        # без -t, пишем пока не остановим
  rec_pid=$!

  # ---------- 2. VNC-действия (идут параллельно с записью) ----------
  vnc_actions "$server" || {
    echo "[${server}] ❌ VNC failed"
    kill -INT "$rec_pid" 2>/dev/null
    wait  "$rec_pid" 2>/dev/null
  }

  sleep "$EXTRA_TIME"
 
  kill -INT "$rec_pid"
  wait "$rec_pid"                           # ждём корректного завершения flvrec

  echo "[${antivirus}] ▶ FFmpeg → ${mp4}"
  ffmpeg -loglevel error -y -i "$flv" -c:v libx264 -preset veryfast "$mp4"
  sleep 1 
  echo "[UP] ▶ smbclient put ${mp4##*/}"
  smbclient "$SHARE" -N -c "lcd $(dirname "$mp4"); put $(basename "$mp4")" || {
    echo "[${antivirus}] ❌ Upload failed"
  }
(
  shutdown_vm        "$vmid"             || { echo "[VM $vmid] ❌ Shutdown failed"; return 1; }
  restore_latest_local  "$vmid"   || { echo "[VM $vmid] ❌ Restore failed";  return 1; }
  start_vm "$vmid"                   || { echo "[VM $vmid] start failed";    return 1; }
  echo "[VM $vmid] ✅ Restored OK"
)>>/var/log/vm_restore.log 2>&1 & disown
}

for server in "${!VM_MAP[@]}"; do
  vnc_shutdown_restore "$server" &
done
wait
echo "✔ Все задачи завершены."
