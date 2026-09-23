# Raspberry Pi SD Backup And Restore

Each dated folder is a complete file-level backup of the Pi's root and boot
filesystems. It preserves Linux ownership, permissions, links, ACLs, and
extended attributes even though the external drive is NTFS.

## Verify a backup

From inside a dated backup folder:

```bash
sha256sum -c SHA256SUMS
```

Only folders containing `COMPLETE` have passed archive and checksum checks.

## Restore to a replacement card

1. Use Raspberry Pi Imager to install the same Raspberry Pi OS architecture
   onto a replacement card, then boot it once and shut it down.
2. Attach both that card and the Passport to another Linux system. Do not
   restore onto a mounted filesystem. Identify the replacement card carefully.
3. Mount its Linux root partition at `/mnt/pi-root` and boot partition at
   `/mnt/pi-root/boot/firmware`.
4. Extract the archives as root:

```bash
sudo tar --acls --xattrs --numeric-owner -I zstd \
  -xpf rootfs.tar.zst -C /mnt/pi-root
sudo tar --acls --xattrs --numeric-owner -I zstd \
  -xpf bootfs.tar.zst -C /mnt/pi-root/boot/firmware
```

5. Compare the new partition UUIDs with `fstab`, `cmdline.txt`, and the saved
   `lsblk.txt`. Update UUID/PARTUUID references if the replacement card differs.
6. Unmount both partitions cleanly and boot the Pi.

The saved `mmcblk0-partitions.sfdisk` records the original partition layout,
and `packages.tsv` records every installed Debian package. They are recovery
references; do not apply the partition table blindly to a different-sized card.

