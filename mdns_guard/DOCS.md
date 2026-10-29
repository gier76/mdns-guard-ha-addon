# mDNS NSEC Guard

Beantwortet AAAA-mDNS-Queries für konfigurierte Fritzbox-Namen mit einer gültigen
negativen Antwort (NSEC „Name existiert, aber kein AAAA“ + A-Record). Damit stoppt
die Retry-Loop von Amazon Fire OS (Echo / Fire TV), die sonst das LAN mit
~110–2600 mDNS-Paketen/s flutet und Wi-Fi-Airtime sowie die CPU von HA-Host/IoT-Geräten
loadet.

## Hintergrund (known bug)
- Echo/Fire TV feuern AAAA-Queries für `*.fritz.box`-Hostnamen ohne Backoff, wenn die
  Antwort unbefriedigend ist: die Fritzbox antwortet mit leerem Paket (`0q 0/0/0`) oder
  nur `fe80::` link-local. Fire OS akzeptiert beides nicht und retryt ewig.
- Gemeldet 28.07.2026 im Amazon Forum / Developer Community, von Amazon bestätigt,
  bis heute unfixed. Reboot der Fritzbox stoppt die Loop nur temporär; Trigger wie
  Geräte-Wake/Sleep starten sie neu.
- Referenzmessungen (nicologiuliani.site/blog/firetv-mdns-storm/): 12 korrekte
  NSEC+A-Antworten silentierten 881 Queries (~300 pps → ~0.1 pps).

## Optionen
- `targets`: Liste `name=ip`, z. B. `FritzBox7590.fritz.box=192.168.1.1`. Nur AAAA-Queries
  für diese Namen werden beantwortet. Nach Änderung: Container neu starten (oder Addon
  liest options.json live neu).
- `interface_name` / `interface_ip`: LAN-Interface der HA-Host-Netzseite (Multicast-Routing
  und IPv6-Scope). Default `enp4s0` / `192.168.1.21`.
- `respond_ipv6`: antwortet auch auf ff02::fb (IPv6-Multicast). Default true.
- `min_interval_ms`: Drossel pro Name (Default 250 ms), damit das Addon selbst nie zur Flut wird.

## Verhalten
- Start: bindet UDP 5353 (IPv4 + IPv6) im Host-Netz, joint 224.0.0.251 / ff02::fb.
- Pro AAAA-Query auf einen target-Namen: sendet NSEC+A multicast (TTL 120s, Flush-Bit),
  loggt jede Antwort ins Log.
- Antwort-Caching auf dem Query-Client (~120s TTL) → der Responder muss dauerhaft laufen
  (`boot: auto`, `startup: system`).

## Verifikation
Nach Start auf dem HA-Host:
```
tcpdump -i enp4s0 -nn -vv 'udp port 5353 and src 192.168.1.21' | grep NSEC
```
und die Query-Rate der Echo/Fire-TV-Devices muss auf <1 pps fallen.
