#!/usr/bin/env python3
"""Homeserver homepage: a live, web version of the login MOTD.

Stdlib only. Serves index.html and /api/stats (JSON). Stats are collected by a
background thread so page requests never block on slow commands.
"""
import json
import os
import platform
import shutil
import socket
import struct
import subprocess
import threading
import time
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.environ.get("HOMEPAGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("HOMEPAGE_PORT", "8088"))
INTERVAL = 5  # seconds between fast stat refreshes
ROOT = Path(__file__).resolve().parent
# In Docker the host filesystem is mounted read-only here (see docker/docker-compose.yml).
# Unset when running directly on the host.
HOST_ROOT = os.environ.get("HOST_ROOT", "")


def host(path):
    """Path on the host, whether we run on it directly or inside the container."""
    return HOST_ROOT.rstrip("/") + path if HOST_ROOT else path


# The container has its own network namespace; the host's is visible through its
# init process (the compose file shares the host PID namespace).
NET = Path(host("/proc/1/net")) if HOST_ROOT else Path("/proc/net")


def hostname():
    if HOST_ROOT:
        try:
            return Path(host("/etc/hostname")).read_text().strip() or socket.gethostname()
        except OSError:
            pass
    return socket.gethostname()


# Friendly labels for containers. Anything not listed falls back to its name.
# "port" overrides the detected port (needed for host-network containers);
# "scheme" defaults to http; "hide" drops it from the services list.
SERVICES = {
    "jellyfin": {"label": "Jellyfin", "group": "media"},
    "immich_server": {"label": "Immich", "group": "media"},
    "qbittorrent": {"label": "qBittorrent", "group": "media", "port": 8080},
    "portainer": {"label": "Portainer", "group": "infra", "scheme": "https", "port": 9443},
    "netdata": {"label": "Netdata", "group": "infra", "port": 19999},
    "nginx-proxy-manager-app-1": {"label": "Proxy Manager", "group": "infra", "port": 81},
    "adguard-home": {"label": "AdGuard Home", "group": "infra"},
    "yorawealth-frontend": {"label": "YoraWealth", "group": "apps"},
    "yoradm-docker-frontend-1": {"label": "YoraDM", "group": "apps"},
    "yorapet-frontend": {"label": "YoraPet", "group": "apps"},
    "yorapet3-api": {"label": "YoraPet3 API", "group": "apps"},
    "tallysync-docker-frontend-1": {"label": "TallySync", "group": "apps"},
    "yoratrader-docker-frontend-1": {"label": "YoraTrader", "group": "apps"},
    # production container uses host networking, so docker reports no port for it
    "homepage": {"label": "Homepage", "group": "apps", "port": 8088},
}
# Host ports that aren't web UIs.
NON_WEB_PORTS = {22, 53, 3306, 5432, 6379, 6881}


def run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


class Cached:
    """Re-run a slow probe at most once per `ttl` seconds.

    An empty result is retried after `retry` seconds instead, so values that
    aren't ready yet at boot (network, Tailscale, Docker) show up quickly.
    """

    def __init__(self, fn, ttl, retry=15):
        self.fn, self.ttl, self.retry, self.value, self.at = fn, ttl, retry, None, 0.0

    def get(self):
        ttl = self.ttl if self.value not in (None, "", [], {}) else self.retry
        if time.monotonic() - self.at > ttl:
            try:
                self.value = self.fn()
            except Exception:  # a broken probe shouldn't take the page down
                pass
            self.at = time.monotonic()
        return self.value


# ── probes ─────────────────────────────────────────────────────────────────

def os_name():
    for line in Path(host("/etc/os-release")).read_text().splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip('"')
    return platform.system()


def banner(font="smslant"):
    if shutil.which("figlet"):
        out = run(["figlet", "-f", font, "-w", "200", hostname()])
        lines = [l.rstrip() for l in out.splitlines() if l.strip()]
        if lines:
            return lines
    return [hostname()]


def _ip4(hexval):
    """/proc/net/route stores addresses as little-endian hex."""
    return int(hexval, 16)


def _ip4_from_str(ip):
    return struct.unpack("<I", socket.inet_aton(ip))[0]


def lan_ip():
    if HOST_ROOT:
        return host_lan_ip()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("1.1.1.1", 80))
            return s.getsockname()[0]
        except OSError:
            return None


def host_lan_ip():
    """The host's address on its default-route interface, from the host's route table."""
    rows = [l.split() for l in (NET / "route").read_text().splitlines()[1:]]
    iface = default_iface()
    subnets = [(_ip4(r[1]), _ip4(r[7])) for r in rows
               if r[0] == iface and r[2] == "00000000" and r[1] != "00000000"]
    local, ip = set(), None
    for line in (NET / "fib_trie").read_text().splitlines():
        line = line.strip()
        if line.startswith("|--"):
            ip = line.split()[1]
        elif line.startswith("/32 host LOCAL") and ip:
            local.add(ip)
    for addr in sorted(local):
        n = _ip4_from_str(addr)
        if any(n & mask == dest for dest, mask in subnets):
            return addr
    return None


def tailscale_cmd(*args):
    # the host's tailscale binary is static, so the container can run it against the host socket
    if HOST_ROOT and Path(host("/usr/bin/tailscale")).exists():
        return [host("/usr/bin/tailscale"), "--socket", host("/run/tailscale/tailscaled.sock"), *args]
    return ["tailscale", *args]


def tailscale_ip():
    out = run(tailscale_cmd("ip", "-4")).split()
    return out[0] if out else None


def meminfo():
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        info[k] = int(v.split()[0]) // 1024  # MB
    total, avail = info["MemTotal"], info["MemAvailable"]
    swap_total = info.get("SwapTotal", 0)
    swap_used = swap_total - info.get("SwapFree", 0)
    return {
        "used": total - avail, "total": total, "pct": round((total - avail) * 100 / total),
        "swap_used": swap_used, "swap_total": swap_total,
        "swap_pct": round(swap_used * 100 / swap_total) if swap_total else None,
    }


def disk(path):
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    used = total - st.f_bfree * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    # same rounding df uses for "Use%"
    pct = -(-used * 100 // (used + avail)) if used + avail else 0
    return {"used": used, "total": total, "pct": pct}


def cpu_temp():
    zones = sorted(Path("/sys/class/thermal").glob("thermal_zone*"))
    for z in zones:
        if (z / "type").read_text().strip() == "x86_pkg_temp":
            return int((z / "temp").read_text()) // 1000
    if zones:
        return int((zones[0] / "temp").read_text()) // 1000
    return None


_prev_cpu = None


def cpu_percent():
    global _prev_cpu
    fields = list(map(int, Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]))
    idle, total = fields[3] + fields[4], sum(fields)
    prev, _prev_cpu = _prev_cpu, (idle, total)
    if not prev or total == prev[1]:
        return None
    return round(100 * (1 - (idle - prev[0]) / (total - prev[1])))


def listening_ports():
    # same count as `ss -tln`: TCP sockets in LISTEN state (0A), IPv4 and IPv6
    count = 0
    for name in ("tcp", "tcp6"):
        try:
            count += sum(1 for l in (NET / name).read_text().splitlines()[1:] if l.split()[3] == "0A")
        except OSError:
            pass
    return count


def ssh_failures():
    journal = ["-D", host("/var/log/journal")] if HOST_ROOT else []
    out = run(["journalctl", *journal, "-u", "ssh", "--since", "24 hours ago", "--no-pager"], timeout=10)
    return out.count("Failed password")


def firewall():
    state = "" if HOST_ROOT else run(["systemctl", "is-active", "ufw"]).strip()
    if not state:
        try:
            conf = Path(host("/etc/ufw/ufw.conf")).read_text()
        except OSError:
            return None
        state = "active" if "ENABLED=yes" in conf else "inactive"
    return state


def top_process():
    out = run(["ps", "-eo", "comm,%cpu", "--sort=-%cpu", "--no-headers"]).splitlines()
    if not out:
        return None
    name, pct = out[0].rsplit(None, 1)
    return f"{name} ({pct}%)"


def smb_shares():
    shares, name = {}, None
    if shutil.which("testparm") and not HOST_ROOT:
        lines = run(["testparm", "-s"], timeout=10).splitlines()
    else:  # no samba tools in the container: read the host's config directly
        try:
            lines = Path(host("/etc/samba/smb.conf")).read_text().splitlines()
        except OSError:
            lines = []
    for line in lines:
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1]
        elif name and line.startswith("path") and "=" in line:
            shares[name] = line.split("=", 1)[1].strip()
    return shares


def drives(shares):
    prefix = host("/mnt/")
    mounts = sorted({l.strip() for l in run(["findmnt", "-rn", "-o", "TARGET"]).splitlines()
                     if l.startswith(prefix)})
    result = []
    for path in mounts:
        try:
            d = disk(path)
        except OSError:
            continue
        m = "/mnt/" + path[len(prefix):]
        d["mount"] = m
        d["smb"] = [n for n, p in (shares or {}).items() if p == m or p.startswith(m + "/")]
        result.append(d)
    return result


def cpu_model():
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            name = line.split(":", 1)[1]
            for junk in ("(R)", "(TM)", "CPU", "Processor"):
                name = name.replace(junk, "")
            return " ".join(name.split())
    return platform.processor() or None


def default_iface():
    for line in (NET / "route").read_text().splitlines()[1:]:
        f = line.split()
        if f[1] == "00000000":
            return f[0]
    return None


_prev_net = None


def network():
    """Default interface, link speed and live throughput (bytes/s)."""
    global _prev_net
    iface = default_iface()
    if not iface:
        return None
    rx = tx = 0
    for line in (NET / "dev").read_text().splitlines()[2:]:
        name, data = line.split(":", 1)
        if name.strip() == iface:
            f = data.split()
            rx, tx = int(f[0]), int(f[8])
    now = time.monotonic()
    prev, _prev_net = _prev_net, (iface, rx, tx, now)
    rate = None
    if prev and prev[0] == iface and now > prev[3]:
        dt = now - prev[3]
        rate = {"rx": max(rx - prev[1], 0) / dt, "tx": max(tx - prev[2], 0) / dt}
    try:
        speed = int(Path(host(f"/sys/class/net/{iface}/speed")).read_text())
    except (OSError, ValueError):
        speed = None
    return {"iface": iface, "speed": speed if speed and speed > 0 else None, "rate": rate}


def apt_updates():
    # in the container, point apt at the host's package state (cache built in memory)
    opts = ["-o", f"Dir={host('/')}", "-o", f"Dir::State::status={host('/var/lib/dpkg/status')}",
            "-o", "Dir::Cache::pkgcache=", "-o", "Dir::Cache::srcpkgcache="] if HOST_ROOT else []
    lines = run(["apt", *opts, "list", "--upgradable"], timeout=20).splitlines()
    pkgs = [l for l in lines if "/" in l and "[upgradable" in l]
    return {"total": len(pkgs), "security": sum("-security" in l for l in pkgs)}


def reboot_required():
    flag = Path(host("/run/reboot-required"))
    if not flag.exists():
        return None
    pkgs = Path(host("/run/reboot-required.pkgs"))
    return sorted(set(pkgs.read_text().split())) if pkgs.exists() else []


def tailscale_status():
    try:
        d = json.loads(run(tailscale_cmd("status", "--json")))
    except ValueError:
        return None
    peers = (d.get("Peer") or {}).values()
    return {
        "state": d.get("BackendState"),
        "online": sum(1 for p in peers if p.get("Online")),
        "peers": len(peers),
    }


def ntp_synced():
    # ask the kernel directly (read-only adjtimex); timedatectl won't run in a container.
    # The clock state is TIME_ERROR (5) while the system clock is unsynchronised.
    try:
        import ctypes, ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        state = libc.adjtimex(ctypes.create_string_buffer(512))
    except (OSError, AttributeError):
        return None
    return None if state < 0 else state != 5


def users_logged_in():
    # Ubuntu 26.04 no longer writes utmp, so `who` is always empty. Read logind's
    # session files instead (loginctl refuses to run inside a container).
    sessions = Path(host("/run/systemd/sessions"))
    if sessions.is_dir():
        users = set()
        for f in sessions.iterdir():
            try:
                kv = dict(l.split("=", 1) for l in f.read_text().splitlines() if "=" in l)
            except OSError:
                continue
            if kv.get("CLASS") == "user" and kv.get("STATE") in ("active", "online"):
                users.add(kv.get("USER"))
        return len(users)
    out = run(["loginctl", "list-sessions", "--no-legend"])
    if out.strip():
        return len({f[2] for f in map(str.split, out.splitlines()) if len(f) > 5 and f[5] == "user"})
    return len({l.split()[0] for l in run(["who"]).splitlines() if l.strip()})


# Unmounted partitions of these types are expected, not spare storage.
SKIP_FSTYPES = {"swap", "LVM2_member", "linux_raid_member", "zfs_member"}


def unmounted():
    """Block devices holding (or able to hold) data that aren't mounted anywhere."""
    out = run(["lsblk", "-J", "-b", "-o",
               "NAME,PATH,SIZE,TYPE,FSTYPE,LABEL,PTTYPE,MOUNTPOINTS,MODEL,TRAN,RM"])
    try:
        devices = json.loads(out)["blockdevices"]
    except (ValueError, KeyError):
        return []
    found = []

    def walk(dev, disk):
        if dev["type"] in ("loop", "rom", "zram"):
            return
        disk = disk or dev
        children = dev.get("children") or []
        for c in children:
            walk(c, disk)
        mounted = any(m for m in (dev.get("mountpoints") or []))
        if children or mounted:
            return
        fstype = dev.get("fstype")
        size = int(dev.get("size") or 0)
        if fstype in SKIP_FSTYPES:
            return
        # EFI system partitions that ship on external drives
        if fstype == "vfat" and size < 1 << 30 and dev["type"] == "part":
            return
        if dev["type"] == "disk":
            state = "no partitions" if not dev.get("pttype") else "empty partition table"
        else:
            state = None
        found.append({
            "path": dev["path"],
            "size": size,
            "fstype": fstype,
            "label": dev.get("label"),
            "model": (disk.get("model") or "").strip() or None,
            "tran": disk.get("tran"),
            "removable": bool(disk.get("rm")),
            "state": state if not fstype else None,
        })

    for d in devices:
        walk(d, None)
    return found


def containers():
    out = run(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=10)
    items = []
    for line in out.splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        name = c["Names"]
        conf = SERVICES.get(name, {})
        port = conf.get("port")
        if port is None:
            for chunk in c.get("Ports", "").split(","):
                chunk = chunk.strip()
                if chunk.startswith("0.0.0.0:") and "->" in chunk:
                    host_port = chunk.split(":", 1)[1].split("->")[0].split("-")[0]
                    if host_port.isdigit() and int(host_port) not in NON_WEB_PORTS:
                        port = int(host_port)
                        break
        status = c.get("Status", "")
        state = c.get("State", "")
        if "unhealthy" in status:
            state = "unhealthy"
        items.append({
            "name": name,
            "label": conf.get("label", name),
            "group": conf.get("group", "other"),
            "port": port,
            "scheme": conf.get("scheme", "http"),
            "state": state,
            "status": status,
            "hidden": conf.get("hide", False),
        })
    return items


# ── collector ──────────────────────────────────────────────────────────────

static = {
    "hostname": hostname(),
    "os": os_name(),
    "kernel": platform.release(),
    "cores": os.cpu_count(),
    "banner": banner(),
    "banner_big": banner("big"),
    "cpu_model": cpu_model(),
}
slow = {
    "lan_ip": Cached(lan_ip, 300),
    "tailscale_ip": Cached(tailscale_ip, 300),
    "ssh_failures": Cached(ssh_failures, 120),
    "firewall": Cached(firewall, 120),
    "shares": Cached(smb_shares, 600),
    "ports": Cached(listening_ports, 30),
    "containers": Cached(containers, 15),
    "unmounted": Cached(unmounted, 60),
    "updates": Cached(apt_updates, 1800),
    "tailscale": Cached(tailscale_status, 60),
    "ntp": Cached(ntp_synced, 300),
    "users": Cached(users_logged_in, 30),
    "docker_version": Cached(lambda: run(["docker", "version", "--format", "{{.Server.Version}}"]).strip() or None, 3600),
}
cpu_history = deque(maxlen=60)
snapshot = {}
lock = threading.Lock()


def collect():
    cpu = cpu_percent()
    if cpu is not None:
        cpu_history.append(cpu)
    with open("/proc/uptime") as f:
        uptime = float(f.read().split()[0])
    data = {
        **static,
        "time": time.time(),
        "uptime": uptime,
        "load": Path("/proc/loadavg").read_text().split()[:3],
        "cpu": cpu,
        "cpu_history": list(cpu_history),
        "memory": meminfo(),
        "root": disk(host("/")),
        "temp": cpu_temp(),
        "top": top_process(),
        "lan_ip": slow["lan_ip"].get(),
        "tailscale_ip": slow["tailscale_ip"].get(),
        "ssh_failures": slow["ssh_failures"].get(),
        "firewall": slow["firewall"].get(),
        "ports": slow["ports"].get(),
        "drives": drives(slow["shares"].get()),
        "unmounted": slow["unmounted"].get() or [],
        "net": network(),
        "processes": sum(1 for p in os.listdir("/proc") if p.isdigit()),
        "updates": slow["updates"].get(),
        "reboot_required": reboot_required(),
        "tailscale": slow["tailscale"].get(),
        "ntp": slow["ntp"].get(),
        "users": slow["users"].get(),
        "docker_version": slow["docker_version"].get(),
        "containers": slow["containers"].get() or [],
    }
    with lock:
        snapshot.clear()
        snapshot.update(data)


def collector_loop():
    while True:
        try:
            collect()
        except Exception as e:
            print("collect failed:", e, flush=True)
        time.sleep(INTERVAL)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "public"), **kwargs)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/stats":
            with lock:
                body = json.dumps(snapshot).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    cpu_percent()  # prime the CPU delta
    time.sleep(0.5)
    collect()
    threading.Thread(target=collector_loop, daemon=True).start()
    print(f"homepage on http://{HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
