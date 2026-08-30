"""Turn a User-Agent string into something a person can read.

The security log stores the raw header, which is 200 characters of vendor
history nobody can scan down a table. This reduces it to "Chrome 141 on
Windows · Desktop", and to a stable *family* ("Chrome on Windows") used to
decide whether a sign-in came from a browser this teacher has used before —
the full string changes with every browser update, so comparing those would
call every Tuesday a new device.

Deliberately small and dependency-free. It is honest about not knowing: an
unrecognised agent returns "Unknown device" rather than a confident guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Order matters: Edge and Opera both claim to be Chrome, Chrome claims to be
# Safari, and Safari claims to be almost everything. Most specific first.
_BROWSERS: list[tuple[str, str]] = [
    ("Edge", r"Edg(?:e|A|iOS)?/(\d+)"),
    ("Opera", r"OPR/(\d+)"),
    ("Samsung Internet", r"SamsungBrowser/(\d+)"),
    ("Firefox", r"(?:Firefox|FxiOS)/(\d+)"),
    ("Chrome", r"(?:Chrome|CriOS)/(\d+)"),
    ("Safari", r"Version/(\d+).*Safari"),
]

_OS: list[tuple[str, str]] = [
    ("Windows 11/10", r"Windows NT 10\.0"),
    ("Windows 8.1", r"Windows NT 6\.3"),
    ("Windows 7", r"Windows NT 6\.1"),
    ("Windows", r"Windows"),
    ("Android", r"Android (\d+)"),
    ("iOS", r"(?:iPhone|iPad).*OS (\d+)"),
    ("macOS", r"Mac OS X"),
    ("Linux", r"Linux"),
]


@dataclass(frozen=True)
class Device:
    browser: str
    browser_version: str
    os: str
    kind: str  # Desktop | Mobile | Tablet | Unknown

    @property
    def label(self) -> str:
        """The full, readable form for a table cell or a details panel."""
        if self.browser == "Unknown" and self.os == "Unknown":
            return "Unknown device"
        browser = (
            f"{self.browser} {self.browser_version}"
            if self.browser_version
            else self.browser
        )
        where = f" on {self.os}" if self.os != "Unknown" else ""
        kind = f" · {self.kind}" if self.kind != "Unknown" else ""
        return f"{browser}{where}{kind}"

    @property
    def family(self) -> str:
        """Version-free identity, for "have they signed in from this before?"."""
        return f"{self.browser} on {self.os}"


def parse(user_agent: str | None) -> Device:
    ua = (user_agent or "").strip()
    if not ua:
        return Device("Unknown", "", "Unknown", "Unknown")

    browser, version = "Unknown", ""
    for name, pattern in _BROWSERS:
        match = re.search(pattern, ua)
        if match:
            browser, version = name, match.group(1)
            break

    os_name = "Unknown"
    for name, pattern in _OS:
        match = re.search(pattern, ua)
        if match:
            os_name = name
            if match.groups():
                os_name = f"{name} {match.group(1)}"
            break

    if re.search(r"iPad|Tablet", ua):
        kind = "Tablet"
    elif re.search(r"Mobi|Android|iPhone", ua):
        kind = "Mobile"
    elif browser != "Unknown" or os_name != "Unknown":
        kind = "Desktop"
    else:
        kind = "Unknown"

    return Device(browser, version, os_name, kind)


def label(user_agent: str | None) -> str:
    return parse(user_agent).label


def family(user_agent: str | None) -> str:
    return parse(user_agent).family
