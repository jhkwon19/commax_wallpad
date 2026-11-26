"""Shared intercom identifiers and wallpad UI checks."""

import re
from dataclasses import dataclass
from shlex import quote
from xml.etree import ElementTree

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN

HOUSEHOLD_PLACE = "2"
LOBBY_PLACE = "5"
CONF_INTERCOM_PACKAGE = "intercom_package"
CONF_INTERCOM_SCREEN = "intercom_screen"
CONF_INTERCOM_UNLOCK = "intercom_unlock"

# Android 4.2 ADB can return status zero even if the remote command fails.
TAP_CONFIRMED = "HA_WALLPAD_TAP_SENT"
READ_SCREEN_COMMAND = (
    'path="/data/local/tmp/ha-intercom-$$.xml"; '
    'trap \'adb shell "rm -f $path" >/dev/null 2>&1\' EXIT; '
    'adb shell dumpsys window windows | grep -F "mCurrentFocus="; '
    'adb shell "uiautomator dump $path >/dev/null 2>&1" && '
    'printf "HA_WALLPAD_SCREEN_EPOCH=%s\n" "$(date +%s)" && adb shell "cat $path"'
)
_SCREEN_EPOCH = re.compile(r"^HA_WALLPAD_SCREEN_EPOCH=(\d{1,12})$", re.MULTILINE)
_PACKAGE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_COMPONENT = re.compile(r"\s([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)(?=\s|})")
_BOUNDS = re.compile(r"\[(\d{1,4}),(\d{1,4})\]\[(\d{1,4}),(\d{1,4})\]")
_CDP_LOBBY_COMPONENT = "com.commax.sipua/com.commax.sipua.SipCallView"
_CDP_LOBBY_TITLE = re.compile(r"(?:[0-9]+\.\s*)?공동현관\s*:\s*호출이 왔습니다")
_CDP_UNLOCK_BOUNDS = "[600,481][688,569]"
_CDP_CALL_BUTTON_BOUNDS = {"[336,481][424,569]", "[468,481][556,569]", _CDP_UNLOCK_BOUNDS}
_CDP_LAYOUT_MARKER = "CDP-1020MB"
_FLOORS = {"공동현관(1층)": "1층", "공동현관(지하1층)": "지하 1층", "공동현관(지하2층)": "지하 2층"}


@dataclass(frozen=True)
class IntercomProfile:
    package: str
    screen_marker: str
    unlock_marker: str

    @classmethod
    def from_options(cls, options) -> "IntercomProfile | None":
        package = options.get(CONF_INTERCOM_PACKAGE, "").strip()
        screen = options.get(CONF_INTERCOM_SCREEN, "").strip()
        unlock = options.get(CONF_INTERCOM_UNLOCK, "").strip()
        if unlock == _CDP_LAYOUT_MARKER:
            if package == _CDP_LOBBY_COMPONENT.split("/", 1)[0] and not screen:
                return cls(package, screen, unlock)
            return None
        if _PACKAGE.fullmatch(package) and screen and unlock and screen != unlock:
            return cls(package, screen, unlock)
        return None


@dataclass(frozen=True)
class UnlockTarget:
    component: str
    captured_at: int
    x: int
    y: int


def intercom_device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}-intercom")},
        name=f"Commax 현관 인터폰 ({entry.data[CONF_HOST]})",
        manufacturer="Commax",
        model="CDP-1020MB",
    )


def lobby_floor(location: str | None) -> str | None:
    return _FLOORS.get(location)


def active_lobby_call(data: dict | None) -> int | None:
    if not data or not data.get("connected") or not data.get("active"):
        return None
    if data.get("place") != LOBBY_PLACE or data.get("last_event") != "ringing":
        return None
    return data.get("last_event_id")


def unlock_target(output: str, profile: IntercomProfile) -> UnlockTarget | None:
    """Require an exact call-screen marker and a unique enabled control."""
    start = output.find("<hierarchy")
    end = output.find("</hierarchy>", start)
    if start < 0 or end < 0 or len(output) > 512_000 or "<!DOCTYPE" in output:
        return None
    prefix = output[:start].replace("\r", "")
    epoch = _SCREEN_EPOCH.search(prefix)
    focus_lines = [line for line in prefix.splitlines() if "mCurrentFocus=" in line]
    focus = _COMPONENT.search(focus_lines[0]) if len(focus_lines) == 1 else None
    if epoch is None or focus is None or focus[1].split("/", 1)[0] != profile.package:
        return None
    try:
        hierarchy = ElementTree.fromstring(output[start:end + len("</hierarchy>")])
    except ElementTree.ParseError:
        return None

    nodes = [
        node for node in hierarchy.iter("node")
        if node.get("package") == profile.package and node.get("visible-to-user") != "false"
    ]

    def matching(marker: str):
        return [
            node for node in nodes
            if marker in (node.get("resource-id"), node.get("text"), node.get("content-desc"))
        ]

    if profile.unlock_marker == _CDP_LAYOUT_MARKER:
        buttons = [
            node for node in nodes if node.get("class") == "android.widget.ImageView"
            and node.get("clickable") == "true" and node.get("enabled") == "true"
        ]
        if (
            focus[1] != _CDP_LOBBY_COMPONENT
            or hierarchy.get("rotation", "0") != "0"
            or not any(node.get("bounds") == "[0,0][1024,600]" for node in nodes)
            or not any(
                node.get("class") == "android.widget.TextView"
                and _CDP_LOBBY_TITLE.fullmatch(node.get("text", ""))
                for node in nodes
            )
            or not _CDP_CALL_BUTTON_BOUNDS <= {node.get("bounds") for node in buttons}
        ):
            return None
        controls = [node for node in buttons if node.get("bounds") == _CDP_UNLOCK_BOUNDS]
    else:
        if not matching(profile.screen_marker):
            return None
        controls = matching(profile.unlock_marker)
    if len(controls) != 1:
        return None
    control = controls[0]
    if control.get("clickable") != "true" or control.get("enabled") != "true":
        return None
    bounds = _BOUNDS.fullmatch(control.get("bounds", ""))
    if bounds is None:
        return None
    left, top, right, bottom = map(int, bounds.groups())
    if right <= left or bottom <= top:
        return None
    return UnlockTarget(focus[1], int(epoch[1]), (left + right) // 2, (top + bottom) // 2)


def tap_command(target: UnlockTarget) -> str:
    """Recheck the focused activity on the router immediately before tapping."""
    expected = quote(f" {target.component}}}")
    return (
        'adb shell dumpsys window windows | grep -F "mCurrentFocus=" '
        f'| grep -Fq -- {expected} && '
        f'now=$(date +%s) && [ "$now" -ge {target.captured_at} ] && '
        f'[ "$now" -le {target.captured_at + 5} ] && '
        f'adb shell "input tap {target.x} {target.y} && echo {TAP_CONFIRMED}"'
    )
