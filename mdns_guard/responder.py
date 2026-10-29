#!/usr/bin/env python3
"""mDNS NSEC Guard.

Amazon Fire OS (Echo / Fire TV) feuert AAAA-mDNS-Queries fuer *.fritz.box-Namen
mit ~110-180 Pakten/s ohne Backoff, wenn die Antwort unbefriedigend ist (leeres
Paket oder nur fe80:: link-local). Dieses Add-on beantwortet genau die
konfigurierten Namen mit einer gueltigen negativen Antwort:

  NSEC("Name existiert, aber kein AAAA") + A-Record als Additional

Fire OS cached die negative Antwort (~120s TTL) und hoert auf zu feuern.
Referenz: Amazon-Forum 'Echos and Fire TVs have a firmware bug that floods the
network with mDNS packets' (gemeldet 2026-07-28, offen) und
nicologiuliani.site/blog/firetv-mdns-storm/ (12 Antworten silentierten 881 Queries).

Stdlib-only, keine Dependencies. host_network: hoert mDNS auf LAN + docker bridge.
"""
import json
import os
import selectors
import socket
import struct
import time

OPTIONS_FILE = "/data/options.json"

TYPE_A, TYPE_AAAA, TYPE_NSEC = 1, 28, 47
CLASS_FLUSH_IN = 0x8001
TTL = 120
V4_GROUP = "224.0.0.251"
V6_GROUP = "ff02::fb"
PORT = 5353


def log(msg):
    print(time.strftime("%H:%M:%S") + " " + msg, flush=True)


def load_options():
    try:
        with open(OPTIONS_FILE) as fh:
            opt = json.load(fh)
    except Exception as err:
        log(f"options.json nicht lesbar ({err}); Defaults genutzt")
        opt = {}
    targets = {}
    for entry in opt.get("targets") or []:
        name, _, ip = str(entry).partition("=")
        name = name.strip().lower().rstrip(".")
        ip = ip.strip()
        if name and ip:
            targets[name] = ip
    return {
        "targets": targets,
        "interface_ip": (opt.get("interface_ip") or "192.168.1.21").strip(),
        "interface_name": (opt.get("interface_name") or "enp4s0").strip(),
        "ipv6": bool(opt.get("respond_ipv6", True)),
        "min_interval": max(50, int(opt.get("min_interval_ms") or 250)) / 1000.0,
    }


def encode_name(name):
    out = b""
    for label in name.split("."):
        out += bytes([len(label)]) + label.encode("latin-1")
    return out + b"\x00"


def decode_name(data, off):
    parts = []
    while off < len(data):
        ln = data[off]
        if ln == 0:
            return ".".join(parts), off + 1
        if ln & 0xC0 == 0xC0:
            ptr = struct.unpack("!H", data[off:off + 2])[0] & 0x3FFF
            parts.append(decode_name(data, ptr)[0])
            return ".".join(parts), off + 2
        if ln > 63:
            return ".".join(parts), off + 1 + ln
        parts.append(data[off + 1:off + 1 + ln].decode("latin-1", "replace"))
        off += 1 + ln
    return ".".join(parts), off


def parse_queries(data):
    """Nur Query-Pakete (Antwort-Bit nicht gesetzt) mit >=1 Frage."""
    if len(data) < 12:
        return []
    if struct.unpack("!H", data[2:4])[0] & 0x8000:
        return []
    qdcount = struct.unpack("!H", data[4:6])[0]
    if qdcount == 0 or qdcount > 8:
        return []
    off = 12
    out = []
    for _ in range(qdcount):
        name, off = decode_name(data, off)
        if off + 4 > len(data):
            break
        qtype = struct.unpack("!H", data[off:off + 2])[0]
        out.append((name.lower().rstrip("."), qtype))
    return out


def record(name, rtype, rdata):
    return encode_name(name) + struct.pack("!HHIH", rtype, CLASS_FLUSH_IN, TTL, len(rdata)) + rdata


def build_response(name, ip):
    """NSEC-Antwort: Name existiert mit A-Record, aber ohne AAAA.
    Bitmap Window 0, Byte 0x40 = Typ-A-Bit."""
    nsec = record(name, TYPE_NSEC, encode_name(name) + bytes([0, 1, 0x40]))
    a = record(name, TYPE_A, socket.inet_aton(ip))
    return struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 1) + nsec + a


def open_v4(iface_ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except OSError:
        pass
    s.bind(("", PORT))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface_ip))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(V4_GROUP) + socket.inet_aton(iface_ip))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    s.setblocking(False)
    return s


def open_v6(iface_name):
    idx = socket.if_nametoindex(iface_name)
    s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except OSError:
        pass
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    s.bind(("", PORT))
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, idx)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP,
                 struct.pack("16sI", socket.inet_pton(socket.AF_INET6, V6_GROUP), idx))
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_LOOP, 0)
    s.setblocking(False)
    return s


def main():
    opt = load_options()
    if not opt["targets"]:
        log("KEINE targets konfiguriert — Addon ist nutzlos, aber läuft trotzdem")
    log(f"targets: {sorted(opt['targets'])}  iface={opt['interface_name']}/{opt['interface_ip']}  ipv6={opt['ipv6']}")

    v4 = None
    v6 = None
    sel = selectors.DefaultSelector()
    last_sent = {}
    opt_mtime = 0.0
    iface_ip = opt["interface_ip"]
    iface_name = opt["interface_name"]
    while True:
        # Sockets (nach Option-Aenderung erneut) aufbauen, mit Retry
        if v4 is None:
            try:
                v4 = open_v4(opt["interface_ip"])
                sel.register(v4, selectors.EVENT_READ, "v4")
                log(f"IPv4 mDNS-Socket auf {V4_GROUP} ({opt['interface_ip']}) aktiv")
            except OSError as err:
                log(f"IPv4-Socket fehlgeschlagen ({err}); Retry in 5s")
                time.sleep(5)
                continue
        if v6 is None and opt["ipv6"]:
            try:
                v6 = open_v6(opt["interface_name"])
                sel.register(v6, selectors.EVENT_READ, "v6")
                log(f"IPv6 mDNS-Socket auf {V6_GROUP} ({opt['interface_name']}) aktiv")
            except OSError as err:
                log(f"IPv6-Socket fehlgeschlagen ({err}); ipv6 vorerst aus")
                opt["ipv6"] = False
        # options.json neu lesen, wenn Supervisor den Container nicht neu gestartet hat
        try:
            mt = os.path.getmtime(OPTIONS_FILE)
            if mt != opt_mtime:
                opt_mtime = mt
                opt = load_options()
                log(f"Optionen neu geladen: targets={sorted(opt['targets'])}")
                if opt["interface_ip"] != iface_ip or opt["interface_name"] != iface_name:
                    iface_ip, iface_name = opt["interface_ip"], opt["interface_name"]
                    if v4 is not None:
                        sel.unregister(v4)
                        v4.close()
                        v4 = None
                    if v6 is not None:
                        sel.unregister(v6)
                        v6.close()
                        v6 = None
                    opt["ipv6"] = bool(opt["ipv6"])
        except OSError:
            pass
        # Polling-Block
        for key, _ in sel.select(timeout=2.0):
            family = key.data
            sock = key.fileobj
            try:
                data, addr = sock.recvfrom(9000)
            except OSError:
                continue
            for name, qtype in parse_queries(data):
                if qtype != TYPE_AAAA or name not in opt["targets"]:
                    continue
                now = time.monotonic()
                if now - last_sent.get(name, 0.0) < opt["min_interval"]:
                    continue
                last_sent[name] = now
                pkt = build_response(name, opt["targets"][name])
                try:
                    v4.sendto(pkt, (V4_GROUP, PORT))
                except OSError as err:
                    log(f"IPv4-Antwort fehlgeschlagen: {err}")
                if v6 is not None:
                    try:
                        v6.sendto(pkt, (V6_GROUP, PORT))
                    except OSError as err:
                        log(f"IPv6-Antwort fehlgeschlagen: {err}")
                log(f"AAAA '{name}' beantwortet: NSEC + A {opt['targets'][name]} (via {family})")


if __name__ == "__main__":
    main()
