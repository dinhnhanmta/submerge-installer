#!/usr/bin/env bash
# Sao luu cau hinh submerge len GitHub, da MA HOA bang mat khau sao luu:
#   submerge-backup
#
# Goi sao luu (config.enc) gom: nguon subscription, danh sach may chu da chon, token,
# va khoa day len GitHub cua kho nay. Mat khau nam o /etc/submerge/backup.pass (chi root doc).
set -euo pipefail

REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PASSFILE=/etc/submerge/backup.pass
DEPLOY_KEY=/root/.ssh/submerge_deploy
CIPHER=(-aes-256-cbc -pbkdf2 -iter 600000 -md sha256)
cd "$REPO"

if [ ! -s "$PASSFILE" ]; then
  echo "Thieu mat khau sao luu. Ghi mat khau (mot dong, khong xuong dong) vao $PASSFILE roi chmod 600."
  exit 1
fi

if [ ! -d .git ]; then
  git init -q -b main
fi
git config user.name >/dev/null 2>&1 || git config user.name "submerge-backup"
git config user.email >/dev/null 2>&1 || git config user.email "submerge-backup@localhost"
chmod 700 "$REPO"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir "$WORK/in"
for f in env sources.txt selected.txt; do
  [ -f "/etc/submerge/$f" ] && install -m 600 "/etc/submerge/$f" "$WORK/in/$f"
done
[ -f "$DEPLOY_KEY" ] && install -m 600 "$DEPLOY_KEY" "$WORK/in/deploy_key"

# Goi tar co noi dung on dinh de biet cau hinh co thay doi that hay khong
tar --sort=name --mtime='2000-01-01 00:00:00' --owner=0 --group=0 --numeric-owner \
    -C "$WORK/in" -cf - . | gzip -n > "$WORK/bundle.tgz"
HASH="$(sha256sum < "$WORK/bundle.tgz" | cut -d' ' -f1)"
if [ ! -f config.enc ] || [ "$(cat .git/submerge-config-hash 2>/dev/null || true)" != "$HASH" ]; then
  openssl enc "${CIPHER[@]}" -salt -pass "file:$PASSFILE" -in "$WORK/bundle.tgz" -out config.enc
  echo "$HASH" > .git/submerge-config-hash
fi

git add -A
if git diff --cached --quiet; then
  echo "Khong co thay doi nao so voi ban sao luu truoc."
else
  git commit -q -m "backup $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "Da ghi ban sao luu moi: $(git log -1 --format=%h' '%s)"
fi

if ! git remote | grep -qx origin; then
  echo "Chua gan kho GitHub nen chua day len duoc."
  exit 1
fi
git push -q -u origin HEAD:main
echo "Da day len GitHub."
