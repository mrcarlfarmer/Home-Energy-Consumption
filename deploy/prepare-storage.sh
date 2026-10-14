#!/bin/sh
# Run on the Linux host, not inside the application container.
set -eu
if [ "$(id -u)" -ne 0 ] || [ "$#" -ne 1 ]; then
    echo "Usage: sudo sh deploy/prepare-storage.sh /absolute/new/storage-root" >&2
    exit 1
fi
root=$1
case "$root" in
    /*) ;;
    *) echo "Storage root must be absolute" >&2; exit 1 ;;
esac
image="$root/energy-data.ext4"
data="$root/data"
if [ -e "$image" ] || [ -e "$data" ]; then
    echo "Refusing existing image or data directory. Use a NEW storage root." >&2
    exit 1
fi
mkdir -p "$root"
available_kib=$(df -Pk "$root" | awk 'NR==2 {print $4}')
if [ "$available_kib" -lt 3145728 ]; then
    echo "Need 2 GiB for the image plus at least 1 GiB remaining host space." >&2
    exit 1
fi
# noclobber also refuses a file created concurrently.
(set -C; : > "$image")
chmod 600 "$image"
fallocate -l 2147483648 "$image"
mkfs.ext4 -q -F -m 0 "$image"
mkdir "$data"
mount -o loop,nodev,nosuid,noexec "$image" "$data"
chown 10001:10001 "$data"
chmod 700 "$data"
echo "Mounted bounded 2 GiB data filesystem at $data"
echo "Configure a persistent host mount before restarting the host; see docs/storage-management.md."
