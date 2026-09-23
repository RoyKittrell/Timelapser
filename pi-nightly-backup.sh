#!/usr/bin/env bash
set -Eeuo pipefail

BACKUP_DRIVE="/media/roy/My Passport"
BACKUP_UUID="C6BEB888BEB87293"
BACKUP_ROOT="$BACKUP_DRIVE/Pi SD Backups"
KEEP_BACKUPS=2
MIN_FREE_GIB=12
LOCK_FILE="/run/lock/timelapser-nightly-backup.lock"
TODAY="$(date +%F)"
FINAL_DIR="$BACKUP_ROOT/$TODAY"
WORK_DIR="$BACKUP_ROOT/.incomplete-$TODAY-$$"

log() {
    printf '%s  %s\n' "$(date --iso-8601=seconds)" "$*"
}

fail_soft() {
    log "SKIP: $*"
    exit 0
}

cleanup() {
    if [[ -d "$WORK_DIR" ]]; then
        rm -rf -- "$WORK_DIR"
    fi
}
trap cleanup EXIT

exec 9>"$LOCK_FILE"
flock -n 9 || fail_soft "another backup process is already active"

[[ "$(id -u)" -eq 0 ]] || fail_soft "backup must run as root"
mountpoint -q "$BACKUP_DRIVE" || fail_soft "backup drive is not mounted"

drive_source="$(findmnt -nro SOURCE --target "$BACKUP_DRIVE" || true)"
drive_uuid="$(lsblk -no UUID "$drive_source" 2>/dev/null | head -n1 | tr -d '[:space:]')"
[[ "$drive_uuid" == "$BACKUP_UUID" ]] || fail_soft "mounted drive UUID is not the configured Passport"

if [[ -f "$FINAL_DIR/COMPLETE" ]]; then
    log "OK: verified backup already exists for $TODAY"
    exit 0
fi

busy_pattern='timelapser_v5\.py|mega4_usb_import\.py|timelapser_v6_beta_usb_import\.py|render_completed_runs\.py|ffmpeg'
if pgrep -af "$busy_pattern" >/dev/null; then
    pgrep -af "$busy_pattern" | sed 's/^/BUSY: /'
    fail_soft "timelapse capture, transfer, or rendering is active"
fi

free_kib="$(df --output=avail "$BACKUP_DRIVE" | tail -n1 | tr -d '[:space:]')"
required_kib=$((MIN_FREE_GIB * 1024 * 1024))
(( free_kib >= required_kib )) || fail_soft "less than ${MIN_FREE_GIB} GiB free on backup drive"

if [[ "${1:-}" == "--check" ]]; then
    log "CHECK OK: drive, identity, free space, and workload state are suitable"
    exit 0
fi

mkdir -p "$BACKUP_ROOT" "$WORK_DIR"
log "START: Raspberry Pi system backup to $WORK_DIR"
sync

{
    echo "created=$(date --iso-8601=seconds)"
    echo "hostname=$(hostname)"
    echo "kernel=$(uname -a)"
    echo "source_root=$(findmnt -nro SOURCE /)"
    echo "source_boot=$(findmnt -nro SOURCE /boot/firmware)"
} >"$WORK_DIR/backup-info.txt"

lsblk -o NAME,PATH,SIZE,FSTYPE,LABEL,UUID,PARTUUID,MOUNTPOINTS >"$WORK_DIR/lsblk.txt"
sfdisk --dump /dev/mmcblk0 >"$WORK_DIR/mmcblk0-partitions.sfdisk"
dpkg-query -W -f='${binary:Package}\t${Version}\n' >"$WORK_DIR/packages.tsv"
cp -a /etc/fstab /boot/firmware/cmdline.txt "$WORK_DIR/"
cp -a /home/roy/Timelapser\ Sept2026/timelapser_v5/PI_BACKUP_RESTORE.md "$WORK_DIR/RESTORE.md"

log "ARCHIVE: root filesystem"
tar --acls --xattrs --numeric-owner --one-file-system \
    --exclude='./dev' --exclude='./dev/*' \
    --exclude='./proc' --exclude='./proc/*' \
    --exclude='./sys' --exclude='./sys/*' \
    --exclude='./run' --exclude='./run/*' \
    --exclude='./tmp' --exclude='./tmp/*' \
    --exclude='./mnt' --exclude='./mnt/*' \
    --exclude='./media' --exclude='./media/*' --exclude='./lost+found' \
    --exclude='./var/tmp/*' --exclude='./var/cache/apt/archives/*' \
    --exclude='./var/log/journal/*' \
    -I 'zstd -1 -T0' -cpf "$WORK_DIR/rootfs.tar.zst" -C / .

log "ARCHIVE: boot filesystem"
tar --acls --xattrs --numeric-owner --one-file-system \
    -I 'zstd -1 -T0' -cpf "$WORK_DIR/bootfs.tar.zst" -C /boot/firmware .

log "VERIFY: reading both archives"
tar -I zstd -tf "$WORK_DIR/rootfs.tar.zst" >/dev/null
tar -I zstd -tf "$WORK_DIR/bootfs.tar.zst" >/dev/null
(
    cd "$WORK_DIR"
    sha256sum rootfs.tar.zst bootfs.tar.zst >SHA256SUMS
    sha256sum -c SHA256SUMS
)

touch "$WORK_DIR/COMPLETE"
mv "$WORK_DIR" "$FINAL_DIR"
log "COMPLETE: $FINAL_DIR ($(du -sh "$FINAL_DIR" | cut -f1))"

mapfile -t old_backups < <(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d \
    -name '20??-??-??' -printf '%f\n' | sort -r | tail -n +$((KEEP_BACKUPS + 1)))
for old in "${old_backups[@]}"; do
    log "ROTATE: removing expired backup $old"
    rm -rf -- "$BACKUP_ROOT/$old"
done

trap - EXIT
