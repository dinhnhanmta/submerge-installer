#!/usr/bin/env bash
# Cai, cap nhat hoac KHOI PHUC submerge thanh dich vu systemd. Chay bang root.
#
# Khoi phuc tren VPS moi (hoi mat khau sao luu):
#   bash <(curl -fsSL https://raw.githubusercontent.com/dinhnhanmta/submerge-installer/main/install.sh)
#
# Cap nhat tren VPS da cai:
#   bash /opt/submerge/install.sh
#
# File cau hinh da co tren may luon duoc giu nguyen, nen chay lai nhieu lan khong sao.
set -euo pipefail

REPO_HTTPS="https://github.com/dinhnhanmta/submerge-installer.git"
REPO_SSH="git@github.com:dinhnhanmta/submerge-installer.git"
REPO_DIR=/opt/submerge
XRAY_DEST=/usr/local/bin/xray-submerge
DEPLOY_KEY=/root/.ssh/submerge_deploy
PASSFILE=/etc/submerge/backup.pass
CIPHER=(-aes-256-cbc -pbkdf2 -iter 600000 -md sha256)

need() {  # need <lenh> <goi apt>
  command -v "$1" >/dev/null 2>&1 && return 0
  if command -v apt-get >/dev/null 2>&1; then
    [ -n "${APT_UPDATED:-}" ] || { apt-get update -qq; APT_UPDATED=1; }
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$2" >/dev/null
  else
    echo "Thieu '$1', hay cai truoc roi chay lai."; exit 1
  fi
}

SELF="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || true)"
DIR="$(cd "$(dirname "${SELF:-/nonexistent}")" 2>/dev/null && pwd || true)"

# --- chay thang tu mang (bash <(curl ...)): tai kho ve roi chay ban trong kho
if [ -z "$DIR" ] || [ ! -f "$DIR/submerge.py" ]; then
  need git git
  if [ -d "$REPO_DIR/.git" ]; then
    git -C "$REPO_DIR" pull -q --ff-only || echo "Luu y: khong cap nhat duoc $REPO_DIR, dung ban dang co."
  elif [ -e "$REPO_DIR" ] && [ -n "$(ls -A "$REPO_DIR" 2>/dev/null)" ]; then
    echo "$REPO_DIR da ton tai nhung khong phai kho git. Hay doi ten no roi chay lai."; exit 1
  else
    git clone -q "$REPO_HTTPS" "$REPO_DIR"
  fi
  exec bash "$REPO_DIR/install.sh" "$@"
fi

need python3 python3

id -u submerge >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin submerge

install -d -m 750 -o root -g submerge /etc/submerge
install -d -m 750 -o submerge -g submerge /var/lib/submerge
install -m 755 "$DIR/submerge.py" /usr/local/bin/submerge.py
ln -sf /usr/local/bin/submerge.py /usr/local/bin/submerge
if [ -f "$DIR/backup.sh" ]; then
  chmod 755 "$DIR/backup.sh"
  ln -sf "$DIR/backup.sh" /usr/local/bin/submerge-backup
fi

# --- may moi + co ban sao luu ma hoa: hoi mat khau va khoi phuc
if [ ! -f /etc/submerge/env ] && [ -f "$DIR/config.enc" ]; then
  need openssl openssl
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT
  restored=""
  for attempt in 1 2 3; do
    if [ -n "${SUBMERGE_BACKUP_PASS:-}" ]; then
      PASS="$SUBMERGE_BACKUP_PASS"
    else
      read -r -s -p "Mat khau sao luu: " PASS < /dev/tty
      echo > /dev/tty
    fi
    rm -rf "$WORK/out"; mkdir -p "$WORK/out"
    if SUBMERGE_BACKUP_PASS="$PASS" openssl enc -d "${CIPHER[@]}" -pass env:SUBMERGE_BACKUP_PASS \
         -in "$DIR/config.enc" -out "$WORK/bundle.tgz" 2>/dev/null \
       && tar -xzf "$WORK/bundle.tgz" -C "$WORK/out" 2>/dev/null \
       && [ -f "$WORK/out/env" ]; then
      restored=1
      break
    fi
    echo "Sai mat khau hoac ban sao luu hong."
    [ -z "${SUBMERGE_BACKUP_PASS:-}" ] || break
  done
  if [ -z "$restored" ]; then
    echo "Khong giai ma duoc ban sao luu. Chua thay doi cau hinh nao; chay lai lenh de thu lai."
    exit 1
  fi
  for f in env sources.txt selected.txt; do
    if [ -f "$WORK/out/$f" ]; then
      install -m 600 "$WORK/out/$f" "/etc/submerge/$f"
      echo "Khoi phuc /etc/submerge/$f"
    fi
  done
  if [ -f "$WORK/out/deploy_key" ] && [ ! -f "$DEPLOY_KEY" ]; then
    install -d -m 700 /root/.ssh
    install -m 600 "$WORK/out/deploy_key" "$DEPLOY_KEY"
    echo "Khoi phuc khoa day len GitHub"
  fi
  ( umask 077; printf '%s' "$PASS" > "$PASSFILE" )
  unset PASS
fi

if [ ! -f /etc/submerge/sources.txt ]; then
  cat > /etc/submerge/sources.txt <<'EOF'
# Moi dong mot subscription ben thu 3, dang:   Ten | URL
# "Ten" dung de chon may chu, nen hay dat ten cho moi nguon va dung doi ten ve sau.
#
# NhaCungCapA | https://example.com/sub/xxxxxxxx
# NhaCungCapB | https://example.org/api/sub/yyyyyyyy
EOF
fi

if [ ! -f /etc/submerge/selected.txt ]; then
  cat > /etc/submerge/selected.txt <<'EOF'
# Cac may chu se xuat hien trong subscription, theo dung thu tu nay.
# Moi dong:  Nguon | Ten may chu   (dung `submerge list` / `submerge pick`)
# File khong co dong nao = phat tat ca may chu cua moi nguon.
EOF
fi

if [ ! -f /etc/submerge/env ]; then
  ( umask 077
    {
      echo "SUBMERGE_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
      echo "SUBMERGE_PORT=8899"
      echo "SUBMERGE_INTERVAL=3600"
      echo "SUBMERGE_UA=v2rayNG/1.9.5"
    } > /etc/submerge/env )
fi

chown root:submerge /etc/submerge/sources.txt /etc/submerge/selected.txt
chmod 640 /etc/submerge/sources.txt /etc/submerge/selected.txt
chown root:root /etc/submerge/env
chmod 600 /etc/submerge/env
if [ -f "$PASSFILE" ]; then chown root:root "$PASSFILE"; chmod 600 "$PASSFILE"; fi

# --- de `submerge-backup` day len GitHub bang khoa rieng cua kho (khong can token)
if [ -d "$DIR/.git" ] && [ -f "$DEPLOY_KEY" ] && command -v git >/dev/null 2>&1; then
  chmod 700 "$DIR"
  git -C "$DIR" config core.sshCommand "ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
  if git -C "$DIR" remote | grep -qx origin; then
    git -C "$DIR" remote set-url origin "$REPO_SSH"
  else
    git -C "$DIR" remote add origin "$REPO_SSH"
  fi
fi

# --- Xray cho lenh `submerge test`: neu 3x-ui chay trong Docker thi chep Xray tu container ra
if [ ! -x "$XRAY_DEST" ] && command -v docker >/dev/null 2>&1; then
  case "$(uname -m)" in
    x86_64) ARCH=amd64 ;;
    aarch64) ARCH=arm64 ;;
    *) ARCH="" ;;
  esac
  if [ -n "$ARCH" ]; then
    for c in $(docker ps -q 2>/dev/null); do
      if docker cp "$c:/app/bin/xray-linux-$ARCH" "$XRAY_DEST" >/dev/null 2>&1; then
        chmod 755 "$XRAY_DEST"
        echo "Da chep Xray tu container $c ra $XRAY_DEST."
        break
      fi
    done
  fi
fi
if [ -x "$XRAY_DEST" ]; then
  sed -i '/^SUBMERGE_XRAY=/d' /etc/submerge/env
  echo "SUBMERGE_XRAY=$XRAY_DEST" >> /etc/submerge/env
fi

cat > /etc/systemd/system/submerge.service <<'EOF'
[Unit]
Description=Gop subscription (submerge)
After=network-online.target
Wants=network-online.target

[Service]
User=submerge
Group=submerge
EnvironmentFile=/etc/submerge/env
ExecStart=/usr/bin/env python3 /usr/local/bin/submerge.py serve
Restart=always
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/submerge

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable submerge >/dev/null
systemctl restart submerge

# shellcheck disable=SC1091
. /etc/submerge/env
IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' || true)"
echo
echo "Da cai xong."
echo "URL de dan vao tab Links cua 3x-ui:  http://${IP:-<IP-VPS>}:${SUBMERGE_PORT}/${SUBMERGE_TOKEN}"
echo "Trang thai:                          curl http://127.0.0.1:${SUBMERGE_PORT}/${SUBMERGE_TOKEN}/status"
echo "Quan ly may chu:                     submerge list | test | pick | unpick"
echo "Sao luu len GitHub:                  submerge-backup"
if [ ! -x "$XRAY_DEST" ] && ! ls /usr/local/x-ui/bin/xray-linux-* >/dev/null 2>&1; then
  echo "Luu y: chua co Xray cho lenh 'submerge test'. Khi 3x-ui da chay, chay lai: bash $REPO_DIR/install.sh"
fi
