#!/usr/bin/env python3
"""submerge - gop nhieu subscription ben thu 3 thanh mot subscription duy nhat.

Dich vu (submerge serve):
    Doc danh sach nguon tu sources.txt (moi dong: "Ten | URL" hoac chi "URL"),
    tai dinh ky o nen, gop + loai trung, roi phuc vu ket qua (base64) tai
        http://<host>:<port>/<SUBMERGE_TOKEN>
    Trang kiem tra:
        http://<host>:<port>/<SUBMERGE_TOKEN>/status

Chon loc (selected.txt, moi dong "Nguon | Ten may chu"):
    Neu file co it nhat mot dong thi CHI nhung may chu do duoc phat ra, theo
    dung thu tu trong file. File rong hoac khong ton tai = phat tat ca.
    Dich vu tu nhan thay doi cua sources.txt va selected.txt sau vai giay.

Lenh quan ly:
    submerge list [tu khoa]     xem danh muc (danh so) va muc nao dang chon
    submerge list -s            chi xem cac muc dang chon
    submerge test [so...]       test ket noi that qua Xray (mac dinh: muc dang chon)
    submerge pick so...         chon them may chu (vd: pick 3 8 20-25)
    submerge unpick so...       bo chon
    submerge url                in link de dan vao tab Links cua 3x-ui

Chi dung thu vien chuan cua Python 3.
"""
import argparse
import base64
import binascii
import glob
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def load_env_file(path="/etc/submerge/env"):
    """Cho lenh quan ly dung chung cau hinh voi dich vu (systemd tu nap file nay)."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                key, sep, value = line.strip().partition("=")
                if sep and key.startswith("SUBMERGE_"):
                    os.environ.setdefault(key, value)
    except OSError:
        pass


load_env_file()

SOURCES = os.environ.get("SUBMERGE_SOURCES", "/etc/submerge/sources.txt")
SELECTED = os.environ.get("SUBMERGE_SELECTED", "/etc/submerge/selected.txt")
STATE = os.environ.get("SUBMERGE_STATE", "/var/lib/submerge/state.json")
TOKEN = os.environ.get("SUBMERGE_TOKEN", "")
HOST = os.environ.get("SUBMERGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("SUBMERGE_PORT", "8899"))
# Ten mien (hoac IP) cong khai cua VPS, chi dung de in ra link cho 3x-ui (`submerge url`).
PUBLIC_HOST = os.environ.get("SUBMERGE_PUBLIC_HOST", "").strip()
INTERVAL = int(os.environ.get("SUBMERGE_INTERVAL", "3600"))  # giay giua 2 lan tai lai nguon
USER_AGENT = os.environ.get("SUBMERGE_UA", "v2rayNG/1.9.5")
# 1 = them "[Ten nguon] " truoc ten moi may chu khi phat ra; 0 = giu nguyen ten goc.
ADD_PREFIX = os.environ.get("SUBMERGE_PREFIX", "1").strip().lower() not in ("0", "false", "no", "off")
TEST_URL = os.environ.get("SUBMERGE_TEST_URL", "https://www.gstatic.com/generate_204")
TEST_TIMEOUT = float(os.environ.get("SUBMERGE_TEST_TIMEOUT", "10"))
# So may chu test cung luc. De thap: nhieu ket noi dong thoi tren cung mot tai khoan
# lam nha cung cap bop toc do, do tre do duoc se bi doi len nhieu lan.
TEST_PARALLEL = max(1, int(os.environ.get("SUBMERGE_TEST_PARALLEL", "3")))
FETCH_TIMEOUT = 15
MAX_BYTES = 4 << 20
PANEL_LINK_LIMIT = 1000  # 3x-ui cat bot neu 1 subscription co hon 1000 link
WATCH_SECONDS = 3  # chu ky kiem tra sources.txt / selected.txt co doi khong

# Cung danh sach scheme ma 3x-ui chap nhan khi doc subscription tu xa.
SCHEMES = {
    "vmess", "vless", "trojan", "ss", "ssr", "hysteria", "hysteria2", "hy2",
    "tuic", "wireguard", "wg", "socks", "socks5", "anytls", "juicity",
    "naive+https",
}

SELECTED_HEADER = (
    "# Cac may chu se xuat hien trong subscription, theo dung thu tu nay.\n"
    "# Moi dong:  Nguon | Ten may chu   (dung `submerge list` / `submerge pick`)\n"
    "# File khong co dong nao = phat tat ca may chu cua moi nguon.\n"
)

snapshot = {"sub": b"", "status": b"chua tai lan nao\n"}


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------- nguon & link

def read_sources():
    """Tra ve danh sach (ten, url) theo dung thu tu trong file."""
    out = []
    with open(SOURCES, encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            name, url = "", line
            left, sep, right = line.partition("|")
            if sep and "://" not in left:
                name, url = left.strip(), right.strip()
            if url.lower().startswith(("http://", "https://")):
                out.append((name, url))
            else:
                log("bo qua dong khong phai URL http(s): %r" % line[:40])
    return out


def b64decode_loose(text):
    text = "".join(text.split()).replace("-", "+").replace("_", "/")
    return base64.b64decode(text + "=" * (-len(text) % 4))


def extract_links(text):
    links = []
    for line in text.splitlines():
        line = line.strip()
        scheme, sep, _ = line.partition("://")
        if sep and scheme.lower() in SCHEMES:
            links.append(line)
    return links


def parse_body(text):
    """Nhan danh sach link dang van ban thuong hoac base64."""
    links = extract_links(text)
    if links:
        return links
    try:
        decoded = b64decode_loose(text).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return []
    return extract_links(decoded)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        body = resp.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("phan hoi lon hon %d MiB" % (MAX_BYTES >> 20))
    return body.decode("utf-8", "replace")


def vmess_config(link):
    """Tra ve dict neu la vmess://<base64 json>, nguoc lai None."""
    try:
        cfg = json.loads(b64decode_loose(link.split("://", 1)[1]))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return cfg if isinstance(cfg, dict) else None


def identity(link):
    """Khoa nhan dien may chu, khong phu thuoc ten hien thi (de loai trung)."""
    scheme = link.split("://", 1)[0].lower()
    if scheme == "vmess":
        cfg = vmess_config(link)
        if cfg is not None:
            cfg.pop("ps", None)
            return "vmess://" + json.dumps(cfg, sort_keys=True)
    if scheme == "ssr":
        return link
    return link.partition("#")[0]


def clean(text):
    return " ".join(str(text).split())


def display_name(link):
    """Ten hien thi cua link (vmess: truong ps; con lai: phan sau dau #)."""
    scheme = link.split("://", 1)[0].lower()
    if scheme == "vmess":
        cfg = vmess_config(link)
        if cfg is not None:
            return clean(cfg.get("ps", ""))
    return clean(urllib.parse.unquote(link.partition("#")[2]))


def with_prefix(link, prefix):
    """Them "[prefix] " vao truoc ten hien thi cua link."""
    if not prefix:
        return link
    tag = "[%s]" % prefix
    scheme = link.split("://", 1)[0].lower()
    if scheme == "vmess":
        cfg = vmess_config(link)
        if cfg is not None:
            cfg["ps"] = ("%s %s" % (tag, cfg.get("ps", ""))).strip()
            raw = json.dumps(cfg, ensure_ascii=False, separators=(",", ":"))
            return "vmess://" + base64.b64encode(raw.encode("utf-8")).decode("ascii")
    if scheme == "ssr":
        return link  # ten nam trong phan base64, giu nguyen
    base, _, frag = link.partition("#")
    name = ("%s %s" % (tag, urllib.parse.unquote(frag))).strip()
    return base + "#" + urllib.parse.quote(name, safe="")


# ------------------------------------------------------------ trang thai luu

def load_state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATE)
    except OSError as exc:
        log("khong luu duoc state: %s" % exc)


# ------------------------------------------------------- danh muc & chon loc

def source_label(name, index):
    return name or ("nguon %d" % index)


def catalog(sources, state):
    """Moi may chu cua moi nguon, da loai trung, theo thu tu nguon."""
    items, seen = [], set()
    for index, (name, url) in enumerate(sources, 1):
        for link in state.get(url, {}).get("links", []):
            key = identity(link)
            if key in seen:
                continue
            seen.add(key)
            items.append({"source": source_label(name, index), "prefix": name,
                          "name": display_name(link), "link": link})
    return items


def read_selected():
    """Danh sach (nguon, ten) da chon, theo thu tu trong file."""
    pins = []
    try:
        with open(SELECTED, encoding="utf-8-sig") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                source, sep, name = line.partition("|")
                pin = (clean(source), clean(name))
                if sep and pin not in pins:
                    pins.append(pin)
    except OSError:
        pass
    return pins


def write_selected(pins):
    # Ghi de tai cho (khong thay file) de giu nguyen chu so huu va quyen cua file.
    with open(SELECTED, "w", encoding="utf-8") as fh:
        fh.write(SELECTED_HEADER)
        for source, name in pins:
            fh.write("%s | %s\n" % (source, name))


def apply_selection(items, pins):
    """Tra ve (cac muc duoc phat, cac muc da chon nhung khong con trong nguon)."""
    if not pins:
        return items, []
    by_pin = {}
    for item in items:
        by_pin.setdefault((item["source"], item["name"]), []).append(item)
    chosen, missing = [], []
    for pin in pins:
        if pin in by_pin:
            chosen.extend(by_pin[pin])
        else:
            missing.append(pin)
    return chosen, missing


def build_snapshot(sources, state):
    items = catalog(sources, state)
    pins = read_selected()
    chosen, missing = apply_selection(items, pins)
    counts = {}
    for item in items:
        counts[item["source"]] = counts.get(item["source"], 0) + 1

    report = []
    for index, (name, url) in enumerate(sources, 1):
        entry = state.get(url, {})
        label = source_label(name, index)
        ok_at = entry.get("ok_at")
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ok_at)) if ok_at else "chua bao gio"
        line = "%-20s %4d link | lan tai tot gan nhat: %s" % (label, counts.get(label, 0), when)
        if entry.get("error"):
            line += " | LOI (dang dung ban cu): %s" % entry["error"]
        report.append(line)
    report.append("")
    report.append("Danh muc sau khi loai trung: %d may chu" % len(items))
    if pins:
        report.append("Che do: CHON LOC - dang phat %d may chu (%d dong trong selected.txt)"
                      % (len(chosen), len(pins)))
        for source, name in missing:
            report.append("  KHONG CON TRONG NGUON: %s | %s" % (source, name))
    else:
        report.append("Che do: PHAT TAT CA (selected.txt chua co dong nao)")
    if len(chosen) > PANEL_LINK_LIMIT:
        report.append("CANH BAO: 3x-ui chi lay %d link dau tien cua moi subscription." % PANEL_LINK_LIMIT)
    merged = [with_prefix(item["link"], item["prefix"]) if ADD_PREFIX else item["link"] for item in chosen]
    sub = base64.b64encode("\n".join(merged).encode("utf-8"))
    return {"sub": sub, "status": ("\n".join(report) + "\n").encode("utf-8")}


# ------------------------------------------------------------------ dich vu

def fetch_source(source):
    _, url = source
    try:
        links = parse_body(fetch(url))
        if not links:
            return None, "khong tim thay link nao (dinh dang khong ho tro?)"
        return links, ""
    except Exception as exc:  # loi mang, HTTP, TLS...
        return None, "%s: %s" % (type(exc).__name__, exc)


def publish(state):
    global snapshot
    snapshot = build_snapshot(read_sources(), state)


def refresh(state):
    sources = read_sources()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch_source, sources))
    now = int(time.time())
    for (_, url), (links, error) in zip(sources, results):
        entry = state.setdefault(url, {"links": [], "ok_at": 0})
        entry["error"] = error
        if links is not None:  # tai loi thi giu nguyen ban tot gan nhat
            entry["links"], entry["ok_at"] = links, now
    wanted = {url for _, url in sources}
    for url in [u for u in state if u not in wanted]:
        del state[url]
    publish(state)
    save_state(state)


def file_stamp(path):
    try:
        st = os.stat(path)
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def refresh_loop(state):
    next_fetch, seen = 0.0, None
    while True:
        current = (file_stamp(SOURCES), file_stamp(SELECTED))
        try:
            if seen is None or time.monotonic() >= next_fetch or current[0] != seen[0]:
                next_fetch = time.monotonic() + INTERVAL
                refresh(state)
            elif current[1] != seen[1]:
                publish(state)  # chi doi lua chon: khong can tai lai nguon
        except Exception as exc:
            log("refresh loi: %s: %s" % (type(exc).__name__, exc))
        seen = current
        time.sleep(WATCH_SECONDS)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/").encode("utf-8", "replace")
        secret = ("/" + TOKEN).encode("utf-8")
        if hmac.compare_digest(path, secret):
            body = snapshot["sub"]
        elif hmac.compare_digest(path, secret + b"/status"):
            body = snapshot["status"]
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # khong ghi log request: duong dan chua token bi mat


def cmd_serve(_args):
    if len(TOKEN) < 16:
        sys.exit("SUBMERGE_TOKEN phai dai it nhat 16 ky tu")
    state = load_state()
    try:
        publish(state)  # phuc vu ngay ban da luu
    except OSError as exc:
        sys.exit("khong doc duoc %s: %s" % (SOURCES, exc))
    threading.Thread(target=refresh_loop, args=(state,), daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log("submerge dang nghe tai %s:%d" % (HOST, PORT))
    server.serve_forever()


# ------------------------------------------------- link -> outbound cua Xray

class Unsupported(Exception):
    pass


def stream_settings(net, security, p):
    """p: host, path, sni, fp, alpn, pbk, sid, spx, serviceName, authority, mode, headerType, extra, insecure."""
    net = {"": "tcp", "raw": "tcp", "splithttp": "xhttp"}.get(net, net)
    host, path = p.get("host") or "", p.get("path") or ""
    out = {"network": net}
    if net == "tcp":
        if p.get("headerType") == "http":
            out["tcpSettings"] = {"header": {"type": "http", "request": {
                "path": [path or "/"], "headers": {"Host": [h for h in host.split(",") if h]}}}}
    elif net == "ws":
        out["wsSettings"] = {"path": path or "/", "host": host}
    elif net == "httpupgrade":
        out["httpupgradeSettings"] = {"path": path or "/", "host": host}
    elif net == "grpc":
        out["grpcSettings"] = {"serviceName": p.get("serviceName") or "",
                               "authority": p.get("authority") or "",
                               "multiMode": p.get("mode") == "multi"}
    elif net == "xhttp":
        xhttp = {"path": path or "/", "host": host, "mode": p.get("mode") or "auto"}
        if p.get("extra"):
            try:
                xhttp["extra"] = json.loads(p["extra"])
            except ValueError:
                pass
        out["xhttpSettings"] = xhttp
    else:
        raise Unsupported("transport '%s'" % net)

    sni = p.get("sni") or ""
    if security == "tls":
        tls = {}
        if sni:
            tls["serverName"] = sni
        if p.get("fp"):
            tls["fingerprint"] = p["fp"]
        if p.get("alpn"):
            tls["alpn"] = [a for a in p["alpn"].split(",") if a]
        if str(p.get("insecure", "")).lower() in ("1", "true"):
            tls["allowInsecure"] = True
        out["security"], out["tlsSettings"] = "tls", tls
    elif security == "reality":
        out["security"] = "reality"
        out["realitySettings"] = {"serverName": sni, "fingerprint": p.get("fp") or "chrome",
                                  "publicKey": p.get("pbk") or "", "shortId": p.get("sid") or "",
                                  "spiderX": p.get("spx") or ""}
    elif security in ("", "none"):
        out["security"] = "none"
    else:
        raise Unsupported("security '%s'" % security)
    return out


def split_url(link):
    u = urllib.parse.urlsplit(link)
    try:
        host, port = u.hostname, u.port
    except ValueError:
        raise Unsupported("dia chi khong hop le")
    if not host or not port:
        raise Unsupported("thieu dia chi hoac cong")
    q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    q["insecure"] = q.get("allowInsecure") or q.get("insecure") or ""
    return u, host, port, q


def to_outbound(link):
    """Doi share link thanh outbound cua Xray; nem Unsupported neu khong doi duoc."""
    scheme = link.split("://", 1)[0].lower()
    if scheme == "vless":
        u, host, port, q = split_url(link)
        user = {"id": urllib.parse.unquote(u.username or ""), "encryption": q.get("encryption") or "none"}
        if q.get("flow"):
            user["flow"] = q["flow"]
        return {"protocol": "vless",
                "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]},
                "streamSettings": stream_settings(q.get("type", ""), q.get("security", ""), q)}
    if scheme == "trojan":
        u, host, port, q = split_url(link)
        return {"protocol": "trojan",
                "settings": {"servers": [{"address": host, "port": port,
                                          "password": urllib.parse.unquote(u.username or "")}]},
                "streamSettings": stream_settings(q.get("type", ""), q.get("security", "tls"), q)}
    if scheme == "vmess":
        cfg = vmess_config(link)
        if cfg is None:
            raise Unsupported("vmess khong phai dang base64-json")
        try:
            port = int(cfg.get("port"))
        except (TypeError, ValueError):
            raise Unsupported("cong khong hop le")
        net = str(cfg.get("net") or "tcp")
        p = {"host": str(cfg.get("host") or ""), "path": str(cfg.get("path") or ""),
             "sni": str(cfg.get("sni") or ""), "fp": str(cfg.get("fp") or ""),
             "alpn": str(cfg.get("alpn") or ""), "headerType": str(cfg.get("type") or "")}
        if net == "grpc":
            p["serviceName"], p["mode"] = p["path"], p["headerType"]
        user = {"id": str(cfg.get("id") or ""), "security": str(cfg.get("scy") or "auto")}
        try:
            user["alterId"] = int(cfg.get("aid") or 0)
        except (TypeError, ValueError):
            user["alterId"] = 0
        return {"protocol": "vmess",
                "settings": {"vnext": [{"address": str(cfg.get("add") or ""), "port": port, "users": [user]}]},
                "streamSettings": stream_settings(net, "tls" if cfg.get("tls") == "tls" else "none", p)}
    if scheme == "ss":
        body = link.split("://", 1)[1].partition("#")[0]
        if "@" not in body:  # dang cu: ss://base64(method:password@host:port)
            try:
                body = b64decode_loose(body.partition("?")[0]).decode("utf-8")
            except (binascii.Error, ValueError):
                raise Unsupported("ss khong giai ma duoc")
        u, host, port, q = split_url("ss://" + body)
        if q.get("plugin"):
            raise Unsupported("ss co plugin")
        userinfo = urllib.parse.unquote(u.username or "")
        if u.password is not None:
            userinfo += ":" + urllib.parse.unquote(u.password)
        if ":" not in userinfo:
            try:
                userinfo = b64decode_loose(userinfo).decode("utf-8")
            except (binascii.Error, ValueError):
                raise Unsupported("ss khong giai ma duoc")
        method, _, password = userinfo.partition(":")
        return {"protocol": "shadowsocks",
                "settings": {"servers": [{"address": host, "port": port,
                                          "method": method, "password": password}]}}
    raise Unsupported("giao thuc '%s'" % scheme)


# ----------------------------------------------------------- test qua Xray

def running_xray():
    """Duong dan file cua tien trinh Xray dang chay (vd: Xray do 3x-ui khoi dong)."""
    for exe in glob.glob("/proc/[0-9]*/exe"):
        try:
            target = os.readlink(exe)
        except OSError:
            continue
        if os.path.basename(target).lower().startswith("xray") and os.path.isfile(target):
            return target
    return ""


def find_xray():
    candidates = [os.environ.get("SUBMERGE_XRAY", "")]
    candidates += sorted(glob.glob("/usr/local/x-ui/bin/xray-linux-*"))
    candidates.append(shutil.which("xray") or "")
    candidates.append(running_xray())
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def free_ports(count):
    socks = []
    try:
        for _ in range(count):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            socks.append(s)
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def wait_listening(proc, port, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def probe(port):
    """Goi TEST_URL qua proxy HTTP cuc bo; tra ve (dat?, ghi chu)."""
    proxy = "http://127.0.0.1:%d" % port
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    note = ""
    for _ in range(2):  # thu lai 1 lan de bo qua loi thoang qua
        start = time.monotonic()
        try:
            with opener.open(TEST_URL, timeout=TEST_TIMEOUT) as resp:
                resp.read(64)
                if resp.status in (200, 204):
                    return True, "%d ms" % ((time.monotonic() - start) * 1000)
                note = "HTTP %d" % resp.status
        except Exception as exc:
            note = clean(getattr(exc, "reason", None) or exc)[:70] or type(exc).__name__
    return False, note


def run_xray(xray, outbounds):
    """Chay 1 tien trinh Xray voi moi outbound mot cong proxy rieng roi do tung cai.

    Tra ve (danh sach ket qua, "") hoac (None, log cua Xray) neu Xray khong khoi dong duoc.
    """
    ports = free_ports(len(outbounds))
    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [{"tag": "in%d" % i, "listen": "127.0.0.1", "port": port, "protocol": "http"}
                     for i, port in enumerate(ports)],
        "outbounds": [dict(ob, tag="out%d" % i) for i, ob in enumerate(outbounds)],
        "routing": {"rules": [{"type": "field", "inboundTag": ["in%d" % i], "outboundTag": "out%d" % i}
                              for i in range(len(outbounds))]},
    }
    tmp = tempfile.mkdtemp(prefix="submerge-")
    try:
        cfg_path = os.path.join(tmp, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        with open(os.path.join(tmp, "xray.log"), "w+", encoding="utf-8", errors="replace") as logf:
            proc = subprocess.Popen([xray, "run", "-c", cfg_path], stdout=logf, stderr=subprocess.STDOUT)
            try:
                if not wait_listening(proc, ports[0]):
                    proc.kill()
                    proc.wait()
                    logf.seek(0)
                    return None, logf.read()[-800:]
                with ThreadPoolExecutor(max_workers=TEST_PARALLEL) as pool:
                    return list(pool.map(probe, ports)), ""
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def last_line(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return clean(lines[-1])[-110:] if lines else "khong ro"  # nguyen nhan cu the nam o cuoi dong


def run_tests(xray, links):
    """Tra ve voi moi link: (True/False/None, ghi chu). None = khong test duoc."""
    results = [None] * len(links)
    batch = []
    for i, link in enumerate(links):
        try:
            batch.append((i, to_outbound(link)))
        except Unsupported as exc:
            results[i] = (None, "khong test duoc: %s" % exc)
    if not batch:
        return results
    outcome, err = run_xray(xray, [ob for _, ob in batch])
    if outcome is not None:
        for (i, _), res in zip(batch, outcome):
            results[i] = res
    elif len(batch) == 1:
        results[batch[0][0]] = (False, "Xray tu choi cau hinh: " + last_line(err))
    else:  # mot cau hinh hong lam hong ca lo: chay rieng tung cai de khoanh vung
        for i, ob in batch:
            single, err = run_xray(xray, [ob])
            results[i] = single[0] if single else (False, "Xray tu choi cau hinh: " + last_line(err))
    return results


# ------------------------------------------------------------ lenh quan ly

def load_catalog():
    try:
        sources = read_sources()
    except OSError as exc:
        sys.exit("khong doc duoc %s: %s" % (SOURCES, exc))
    items = catalog(sources, load_state())
    if not items:
        sys.exit("Danh muc dang rong: dich vu chua tai duoc nguon nao (xem trang /status).")
    return items


def endpoint(link):
    scheme = link.split("://", 1)[0].lower()
    if scheme == "vmess":
        cfg = vmess_config(link)
        if cfg is not None:
            return "vmess %s:%s" % (cfg.get("add", "?"), cfg.get("port", "?"))
    try:
        u = urllib.parse.urlsplit(link)
        return "%s %s:%s" % (scheme, u.hostname or "?", u.port or "?")
    except ValueError:
        return scheme


def parse_numbers(tokens, limit):
    numbers = []
    for token in tokens:
        first, sep, last = token.partition("-")
        try:
            lo = int(first)
            hi = int(last) if sep else lo
        except ValueError:
            sys.exit("khong hieu so thu tu: %r" % token)
        if not 1 <= lo <= hi <= limit:
            sys.exit("so thu tu ngoai pham vi 1..%d: %r" % (limit, token))
        numbers.extend(n for n in range(lo, hi + 1) if n not in numbers)
    return numbers


def cmd_list(args):
    items = load_catalog()
    pins = read_selected()
    _, missing = apply_selection(items, pins)
    needle = (args.filter or "").lower()
    shown = 0
    for number, item in enumerate(items, 1):
        picked = (item["source"], item["name"]) in pins
        if args.selected and not picked:
            continue
        if needle and needle not in ("%s %s" % (item["source"], item["name"])).lower():
            continue
        shown += 1
        print("%4d %s %s | %s   (%s)" % (number, "*" if picked else " ", item["source"],
                                         item["name"], endpoint(item["link"])))
    print()
    print("%d/%d may chu duoc hien; dau * = dang chon (%d dong trong selected.txt)."
          % (shown, len(items), len(pins)))
    if not pins:
        print("Chua chon gi: subscription dang phat TAT CA.")
    for source, name in missing:
        print("DA CHON NHUNG KHONG CON TRONG NGUON: %s | %s" % (source, name))


def cmd_pick(args):
    items = load_catalog()
    pins = read_selected()
    remove = args.cmd == "unpick"
    for number in parse_numbers(args.numbers, len(items)):
        item = items[number - 1]
        pin = (item["source"], item["name"])
        if remove and pin in pins:
            pins.remove(pin)
            print("bo chon: %s | %s" % pin)
        elif not remove and pin not in pins:
            pins.append(pin)
            print("da chon: %s | %s" % pin)
    write_selected(pins)
    print("selected.txt hien co %d dong; dich vu tu ap dung sau vai giay." % len(pins))
    if not pins:
        print("Luu y: khong con dong nao => subscription phat TAT CA may chu.")


def cmd_test(args):
    items = load_catalog()
    numbered = list(enumerate(items, 1))
    if args.numbers:
        wanted = parse_numbers(args.numbers, len(items))
        targets = [numbered[n - 1] for n in wanted]
    else:
        pins = read_selected()
        targets = [(n, it) for n, it in numbered if (it["source"], it["name"]) in pins] or numbered
    xray = find_xray()
    if not xray:
        sys.exit("Khong tim thay Xray. Dat SUBMERGE_XRAY=/duong/dan/toi/xray trong /etc/submerge/env.")
    print("Dang test %d may chu qua %s -> %s" % (len(targets), xray, TEST_URL))
    results = run_tests(xray, [it["link"] for _, it in targets])
    tally = {True: 0, False: 0, None: 0}
    for (number, item), (ok, note) in zip(targets, results):
        tally[ok] += 1
        word = {True: "DAT ", False: "HONG", None: "?   "}[ok]
        print("%4d %s %s | %s   -> %s" % (number, word, item["source"], item["name"], note))
    print()
    print("Dat: %d   Hong: %d   Khong test duoc: %d" % (tally[True], tally[False], tally[None]))
    return 1 if tally[False] else 0


def cmd_url(_args):
    if len(TOKEN) < 16:
        sys.exit("Khong doc duoc SUBMERGE_TOKEN (can chay bang root).")
    print("http://%s:%d/%s" % (PUBLIC_HOST or "<IP-hoac-ten-mien-VPS>", PORT, TOKEN))


def main():
    parser = argparse.ArgumentParser(prog="submerge", description="Gop va chon loc subscription.")
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="lenh")
    sub.add_parser("serve", help="chay dich vu HTTP (systemd goi lenh nay)")
    sub.add_parser("url", help="in link de dan vao tab Links cua 3x-ui")
    p = sub.add_parser("list", help="xem danh muc may chu")
    p.add_argument("-s", "--selected", action="store_true", help="chi hien muc dang chon")
    p.add_argument("filter", nargs="?", help="loc theo ten nguon / ten may chu")
    p = sub.add_parser("test", help="test ket noi that qua Xray")
    p.add_argument("numbers", nargs="*", help="so thu tu trong `list` (bo trong = muc dang chon)")
    for name, text in (("pick", "chon may chu de phat"), ("unpick", "bo chon may chu")):
        p = sub.add_parser(name, help=text)
        p.add_argument("numbers", nargs="+", help="so thu tu trong `list`, vd: 3 8 20-25")
    args = parser.parse_args()
    if args.cmd != "serve":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(errors="replace")  # terminal khong phai UTF-8 van in duoc
            except (AttributeError, ValueError):
                pass
    handlers = {"serve": cmd_serve, "url": cmd_url, "list": cmd_list, "test": cmd_test,
                "pick": cmd_pick, "unpick": cmd_pick}
    sys.exit(handlers[args.cmd](args) or 0)


if __name__ == "__main__":
    main()
