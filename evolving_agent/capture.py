"""Bounded, read-only packet capture evidence inspection.

The inspector accepts both classic libpcap and PCAPNG through Scapy's streaming
reader.  It deliberately never writes extracted packet payloads: an evidence
summary normally needs endpoints, protocols, timing, DNS names, and HTTP
request lines rather than a potentially enormous or sensitive byte stream.
"""

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scapy.all import DNS, DNSQR, ICMP, IP, IPv6, TCP, UDP, Ether, Raw  # type: ignore[import-untyped]
from scapy.utils import PcapReader  # type: ignore[import-untyped]

_MAX_FILE_BYTES = 512 * 1024 * 1024
_MAX_PACKETS = 20_000
_MAX_LISTED_PACKETS = 40
_MAX_FLOWS = 30


class CaptureError(Exception):
    """The capture could not be safely inspected."""


def inspect_capture(path: Path, packet: int | None = None) -> str:
    """Return a bounded protocol/flow summary, or one selected packet.

    Reading is streaming, with a packet ceiling, so a capture cannot consume
    unbounded memory or make the agent spend its whole run on one attachment.
    """
    if not path.is_file():
        raise CaptureError(f"Capture file does not exist: {path.name}")
    size = path.stat().st_size
    if size > _MAX_FILE_BYTES:
        raise CaptureError(f"Capture is {size} bytes; the inspection limit is {_MAX_FILE_BYTES} bytes.")
    if packet is not None and (not isinstance(packet, int) or isinstance(packet, bool) or packet < 1):
        raise CaptureError("packet must be a positive one-based integer.")

    protocols: Counter[str] = Counter()
    flows: Counter[str] = Counter()
    listed: list[str] = []
    selected: str | None = None
    count = 0
    first_time: float | None = None
    last_time: float | None = None
    try:
        with PcapReader(str(path)) as reader:
            for frame in reader:
                count += 1
                if count > _MAX_PACKETS:
                    break
                timestamp = _timestamp(frame)
                if timestamp is not None:
                    first_time = timestamp if first_time is None else min(first_time, timestamp)
                    last_time = timestamp if last_time is None else max(last_time, timestamp)
                summary, protocol, flow = _packet_summary(frame, timestamp)
                protocols[protocol] += 1
                if flow:
                    flows[flow] += 1
                if len(listed) < _MAX_LISTED_PACKETS:
                    listed.append(f"{count}: {summary}")
                if packet == count:
                    selected = summary
    except (OSError, EOFError, ValueError, struct_error()) as invalid:
        raise CaptureError(f"Could not read this as a PCAP/PCAPNG capture: {invalid}") from invalid
    except Exception as invalid:  # Scapy uses several parser-specific exception types.
        raise CaptureError(f"Could not decode this PCAP/PCAPNG capture: {invalid}") from invalid

    if count == 0:
        raise CaptureError("The capture contains no packets.")
    truncated = count > _MAX_PACKETS
    observed = min(count, _MAX_PACKETS)
    if packet is not None:
        if selected is None:
            limit = _MAX_PACKETS if truncated else observed
            raise CaptureError(f"Packet {packet} was not found; this capture has {limit}{' or more' if truncated else ''} packets.")
        return f"Packet {packet} of {observed}{'+' if truncated else ''}: {selected}"

    duration = "unknown"
    if first_time is not None and last_time is not None:
        duration = f"{last_time - first_time:.6f}s ({_format_time(first_time)} to {_format_time(last_time)})"
    lines = [
        f"Capture: {path.name}",
        f"Packets inspected: {observed}{' (stopped at safety limit)' if truncated else ''}",
        f"Time span: {duration}",
        "Protocols: " + ", ".join(f"{name}={number}" for name, number in protocols.most_common()),
    ]
    if flows:
        lines.append("Top conversations:")
        lines.extend(f"  {flow} ({number} packets)" for flow, number in flows.most_common(_MAX_FLOWS))
    lines.append(f"First {len(listed)} packets:")
    lines.extend(f"  {item}" for item in listed)
    return "\n".join(lines)


def _packet_summary(frame: Any, timestamp: float | None) -> tuple[str, str, str | None]:
    prefix = f"{_format_time(timestamp)} " if timestamp is not None else ""
    if IP in frame:
        source, destination = str(frame[IP].src), str(frame[IP].dst)
    elif IPv6 in frame:
        source, destination = str(frame[IPv6].src), str(frame[IPv6].dst)
    elif Ether in frame:
        source, destination = str(frame[Ether].src), str(frame[Ether].dst)
    else:
        return prefix + "unrecognized link-layer packet", "Other", None
    if TCP in frame:
        layer = frame[TCP]
        detail = f"TCP {source}:{layer.sport} -> {destination}:{layer.dport} flags={layer.sprintf('%TCP.flags%')}"
        app = _application_detail(frame)
        return prefix + detail + app, "TCP", f"TCP {source}:{layer.sport} ↔ {destination}:{layer.dport}"
    if UDP in frame:
        layer = frame[UDP]
        detail = f"UDP {source}:{layer.sport} -> {destination}:{layer.dport}"
        app = _application_detail(frame)
        return prefix + detail + app, "UDP", f"UDP {source}:{layer.sport} ↔ {destination}:{layer.dport}"
    if ICMP in frame:
        layer = frame[ICMP]
        return prefix + f"ICMP {source} -> {destination} type={layer.type} code={layer.code}", "ICMP", f"ICMP {source} ↔ {destination}"
    return prefix + f"IP {source} -> {destination}", "IP", f"IP {source} ↔ {destination}"


def _application_detail(frame: Any) -> str:
    if DNS in frame:
        dns = frame[DNS]
        if dns.qr == 0 and DNSQR in dns:
            name = bytes(dns[DNSQR].qname).decode("utf-8", "replace").rstrip(".")
            return f" DNS query={name} type={dns[DNSQR].qtype}"
        return f" DNS response rcode={dns.rcode} answers={dns.ancount}"
    if Raw in frame:
        payload = bytes(frame[Raw].load)[:300]
        first = payload.splitlines()[0] if payload else b""
        try:
            text = first.decode("ascii")
        except UnicodeDecodeError:
            return ""
        if text.startswith(("GET ", "POST ", "PUT ", "DELETE ", "HEAD ", "HTTP/")):
            return f" HTTP={text[:180]!r}"
    return ""


def _timestamp(frame: Any) -> float | None:
    try:
        return float(frame.time)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _format_time(value: float | None) -> str:
    if value is None:
        return "unknown-time"
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds")
    except (OverflowError, OSError, ValueError):
        return f"{value:.6f}"


def struct_error() -> type[Exception]:
    """Avoid importing an implementation detail at every packet operation."""
    import struct
    return struct.error
