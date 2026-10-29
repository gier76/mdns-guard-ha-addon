# mDNS NSEC Guard — Home Assistant Add-on

Beantwortet AAAA-mDNS-Queries von Amazon Echo/Fire TV für konfigurierte Hostnamen mit
einer **gültigen negativen Antwort** (NSEC + A-Record) und stoppt damit die
mDNS-Query-Loop, die sonst das ganze LAN mit bis zu ~2600 Paketen/s flutet.

## Das Problem (bekannter Fire-OS-Firmware-Bug)

Echo- und Fire-TV-Geräte (Fire OS) feuern seit einem Firmware-Update (Juli 2026)
unaufhörlich AAAA-mDNS-Queries (z. B. für `FritzBox7590.fritz.box` oder Hostnamen im
Netz) mit 110–2600 Paketen/s **ohne Backoff**:

- Die FRITZ!Box antwortet auf diese Queries mit einem **leeren Paket**
  (`0* [0q] 0/0/0`) oder nur mit der **link-local IPv6** `fe80::…`.
- Fire OS akzeptiert weder „leere Antwort“ noch link-local als gültige Antwort und
  **retryt ewig** — die Anfrage brennt jede ~200 ms erneut auf den Multicast-Group
  `224.0.0.251` / `ff02::fb`.
- Multicast flutet alle Geräte im LAN: Wi-Fi-Airtime wird gefressen, und jeder
  mDNS-Listener (Home Assistant Zeroconf, HAOS-Host, Docker-Bridge-Flood) antwortet
  und erzeugt Gegen-Traffic. Auf einem HAOS-Host sah man so CPU 10–13 % nur wegen
  dieses Traffic.
- Ein Reboot der Fritzbox resetet die Replies nur temporär; Wake/Sleep eines
  Geräts oder ein VPN-Connect startet die Loop neu.

Gemeldet und von Amazon bestätigt, bis heute nicht gefixt:
- [Amazon Forum: „Echos and Fire TVs have a firmware bug that floods the network with mDNS packets“](https://www.amazonforum.com/s/question/0D5at00000n0KEECA2/echos-and-fire-tvs-have-a-firmware-bug-that-floods-the-network-with-mdns-packets)
- [Amazon Developer Community #28820](https://community.amazondeveloper.com/t/possible-fire-os-networking-issue-repeated-mdns-aaaa-queries-2-600-mdns-packets-sec-can-overload-entire-local-networks/28820)
- [ip-phone-forum Thread 326505 (DE)](https://www.ip-phone-forum.de/threads/fehlerhafte-echo-dot-firmware-flutet-das-heimnetzwerk-mit-mdns-anfragen.326505/)

## Die Lösung: eine Antwort, die Fire OS akzeptiert

Ein mDNS-Responder im LAN (dieses Add-on, `host_network: true`) beantwortet die
AAAA-Query für einen konfigurierten Namen mit:

```
0*- [0q] 1/0/1 <name>. (Cache flush) NSEC  ar: <name>. (Cache flush) A <ip>
```

also einer **NSEC-Antwort** im Sinne von „dieser Name existiert (als A-Record),
aber es gibt kein AAAA“ plus dem echten A-Record als Additional-Record, TTL 120 s,
Cache-flush-Bit. Fire OS akzeptiert diese Antwort als gültig und **cached sie ~120 s**
→ während der Cache-Zeit kommt gar keine Query mehr. Referenzmessung eines
vergleichbaren Responders: 12 Antworten silentierten 881 Queries (~300 pps → ~0.1 pps).

Nach Ablauf des TTL kommt eine einzelne Query — die Antwort geht wieder raus, der
Cycle ist damit harmlos (~1 Query/120 s statt Dauerflut).

## Funktionsweise des Add-ons

- `responder.py` ist **stdlib-only Python** (kein pip, keine Dependencies).
- Bindet UDP 5353 im Host-Netzwerk (IPv4 + IPv6, `SO_REUSEPORT`) und joined
  `224.0.0.251` / `ff02::fb` auf dem konfigurierten Interface.
- Parset jedes mDNS-**Query**-Paket (Antwort-Bit unsetzt, qdcount ≥ 1) und matcht
  AAAA-Queries gegen die `targets`-Liste (case-insensitive, trailing-dot-normalisiert).
- Pro Match (gedrosselt über `min_interval_ms`) wird die NSEC+A-Antwort auf beide
  Multicast-Groups gesendet und geloggt.
- `options.json` wird live neu geladen (mtime-Check), Interface-/Socket-Neubau bei
  Interface-Änderung ohne Container-Neustart.
- `startup: system` → startet vor HA Core, `boot: auto` → überlebt Reboots.

## Optionen

| Option | Default | Bedeutung |
|---|---|---|
| `targets` | Fritzbox-Namen | Liste `name=ip` — nur AAAA-Queries dieser Namen werden beantwortet |
| `interface_name` | `enp4s0` | LAN-Interface des Hosts (IPv6-Scope/Multicast-Routing) |
| `interface_ip` | `192.168.1.21` | IPv4-Quelle für Multicast |
| `respond_ipv6` | `true` | auch auf `ff02::fb` antworten |
| `min_interval_ms` | `250` | Drossel pro Name (der Responder flutet nie selbst) |

Beispiel (Targets erweitern, z. B. einen von Echos gepollten Laptop-Namen):

```yaml
targets:
  - FritzBox7590.fritz.box=192.168.1.1
  - HP-EliteBook.local=192.168.1.56
```

Nach Optionsänderung im HA-UI den Add-on neu starten (oder der Responder liest
`/data/options.json` beim nächsten mtime-Change live).

## Installation

1. Add-on Store: diese Repo als Repository hinzufügen (`https://github.com/gier76/mdns-guard-ha-addon`).
2. „mDNS NSEC Guard“ installieren und starten.
3. `targets` auf die im Netz gepollten Namen setzen (die Add-on-Logs zeigen, welche Namen queried werden).

Arch: amd64 (HAOS on x86-64). Der Responder braucht `host_network` — er antwortet im
LAN, nicht im Docker-Netz.

## Verifikation

```bash
# auf dem HAOS-Host:
tcpdump -i enp4s0 -nn -vv 'udp port 5353 and src <host-ip>' | grep NSEC
# Query-Rate der betroffenen Geräte muss auf <1 pps fallen
tcpdump -i enp4s0 -nn 'udp port 5353' | grep 'AAAA (QM)' | wc -l
```

Die Add-on-Logs (`supervisor → Add-on → Logs`) loggen jede Antwort:
`AAAA 'fritzbox7590.fritz.box' beantwortet: NSEC + A 192.168.1.1 (via v4)`.

## Grenzen / Hinweise

- Antworten nur für Namen mit bekannter IPv4 sinnvoll — ein A-Record muss zum
  echten Host passen, sonst werden mDNS-Clients fehlgeleitet.
- Der Responder beantwortet nur AAAA; A/SRV/TXT-Queries bleiben unberührt.
- Alternative Workarounds: Echo/Fire TV ins Fritzbox-Gastnetz mit Client-Isolation
  verbannen, oder auf Amazons Firmware-Fix warten.
