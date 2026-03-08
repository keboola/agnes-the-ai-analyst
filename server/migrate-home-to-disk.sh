#!/bin/bash
# Migrate /home from system disk (sda) to dedicated home disk (sdc)
#
# Prerequisites:
#   1. Create and attach disk via gcloud:
#      gcloud compute disks create home-disk \
#        --project=kids-ai-data-analysis \
#        --zone=europe-north1-a \
#        --size=30GB \
#        --type=pd-balanced
#
#      gcloud compute instances attach-disk data-broker-for-claude \
#        --project=kids-ai-data-analysis \
#        --zone=europe-north1-a \
#        --disk=home-disk
#
#   2. Run this script as root:
#      sudo ./server/migrate-home-to-disk.sh
#
# What it does:
#   - Formats the new disk (ext4)
#   - Copies all home directories preserving permissions
#   - Updates /etc/fstab for persistent mount
#   - Mounts the new disk as /home
#   - Keeps old data in /home.old for manual cleanup

set -euo pipefail

DEVICE="/dev/sdc"
MOUNT_POINT="/home"
TEMP_MOUNT="/mnt/newhome"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

error() {
    log "ERROR: $*"
    exit 1
}

# Must be root
if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root"
fi

# Check device exists
if [[ ! -b "$DEVICE" ]]; then
    error "Device $DEVICE not found. Did you attach the disk via gcloud?"
fi

# Check device is not already mounted
if mount | grep -q "$DEVICE"; then
    error "Device $DEVICE is already mounted"
fi

# Confirm
log "This will:"
log "  1. Format $DEVICE as ext4"
log "  2. Copy /home to the new disk"
log "  3. Mount the new disk as /home"
log ""
read -p "Continue? (yes/no): " CONFIRM
if [[ "$CONFIRM" != "yes" ]]; then
    log "Aborted."
    exit 0
fi

# Step 1: Format
log "Formatting $DEVICE as ext4..."
mkfs.ext4 -m 1 "$DEVICE"

# Step 2: Temporary mount
log "Mounting $DEVICE to $TEMP_MOUNT..."
mkdir -p "$TEMP_MOUNT"
mount "$DEVICE" "$TEMP_MOUNT"

# Step 3: Copy home directories
log "Copying /home to new disk..."
rsync -a --progress /home/ "$TEMP_MOUNT/"

# Step 4: Verify copy
log "Verifying copy..."
ORIG_COUNT=$(find /home -type f | wc -l)
COPY_COUNT=$(find "$TEMP_MOUNT" -type f | wc -l)
if [[ "$ORIG_COUNT" -ne "$COPY_COUNT" ]]; then
    error "File count mismatch: original=$ORIG_COUNT, copy=$COPY_COUNT"
fi
log "File count matches: $ORIG_COUNT files"

# Step 5: Unmount temp
umount "$TEMP_MOUNT"
rmdir "$TEMP_MOUNT"

# Step 6: Rename old home
log "Renaming /home to /home.old..."
mv /home /home.old
mkdir /home

# Step 7: Add to fstab
DISK_UUID=$(blkid -s UUID -o value "$DEVICE")
log "Adding to /etc/fstab with UUID=$DISK_UUID..."
echo "UUID=$DISK_UUID /home ext4 discard,defaults,nofail 0 2" >> /etc/fstab

# Step 8: Mount
log "Mounting /home..."
mount /home

# Step 9: Verify
log "Verifying mount..."
if mount | grep -q "$DEVICE on /home"; then
    log "SUCCESS: $DEVICE is mounted on /home"
else
    error "Mount verification failed!"
fi

# Verify user access
log "Verifying user directories..."
for dir in /home/*/; do
    username=$(basename "$dir")
    if [[ -d "$dir/.ssh" ]]; then
        log "  OK: $username (has .ssh)"
    else
        log "  OK: $username"
    fi
done

log ""
log "Migration complete!"
log ""
log "Next steps:"
log "  1. Test SSH login for at least one user"
log "  2. If everything works, remove old data:"
log "     sudo rm -rf /home.old"
log "  3. Set up snapshot schedule:"
log "     ./server/setup-snapshot-schedule.sh"
