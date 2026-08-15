"""Per-instance phone profile generation & application.

Each emulator instance gets a realistic, unique set of device properties so
they look like different physical phones (useful for multi-account setups):

    IMEI, IMSI, SIM serial (ICCID), Android ID, MAC, phone number,
    manufacturer + model, resolution, CPU/RAM.

``apply_profile`` drives LDPlayer's ``modify`` command which writes these into
the instance's own config (``vms\\leidianN.config``) — independent per instance.
"""

from __future__ import annotations

import random
import string

from dataclasses import dataclass, asdict

from .console import LdConsole


@dataclass
class DeviceProfile:
    imei: str
    imsi: str
    simserial: str
    androidid: str
    mac: str
    pnumber: str
    manufacturer: str
    model: str
    cpu: int
    memory: int
    resolution: str

    def summary(self) -> str:
        return (f"{self.manufacturer} {self.model} | {self.resolution} | "
                f"{self.cpu}c/{self.memory}MB | imei {self.imei} | "
                f"and.id {self.androidid}")


# Realistic vendor pools
VENDORS: dict[str, dict] = {
    "samsung": {
        "manufacturer": "samsung",
        "models": ["SM-S9280", "SM-G991B", "SM-A546B", "SM-N986B",
                   "SM-F946B"],
    },
    "google": {
        "manufacturer": "Google",
        "models": ["Pixel 8", "Pixel 7 Pro", "Pixel 6a", "Pixel 5"],
    },
    "xiaomi": {
        "manufacturer": "Xiaomi",
        "models": ["M2012K11AC", "2107119DC", "2201117TG", "Mi 11 Pro"],
    },
    "oneplus": {
        "manufacturer": "OnePlus",
        "models": ["ONEPLUS A6013", "IN2013", "NE2210", "KB2000"],
    },
    "honor": {
        "manufacturer": "HONOR",
        "models": ["LSA-AN00", "BKL-AL20", "DUB-AL00", "PCT-AL10"],
    },
    "oppo": {
        "manufacturer": "OPPO",
        "models": ["CPH2601", "CPH2451", "PGJM10", "CPH2223"],
    },
    "vivo": {
        "manufacturer": "vivo",
        "models": ["V2156A", "V2054A", "V2006", "I2012"],
    },
    "nokia": {
        "manufacturer": "Nokia",
        "models": ["TA-1257", "TA-1196", "TA-1357", "TA-1303"],
    },
}

# MCC/MNC pools (country code + network code) for realistic IMSI/ICCID
IMSI_PREFIXES = ["46000", "46001", "46002", "46003", "46007",  # China
                 "310260", "310030", "310150",  # US
                 "23415", "23410",  # UK
                 "26201", "26202",  # Germany
                 "40400", "40401",  # India
                 "44010", "44020",  # Japan
                 "90101", "90102",  # MTN South Africa
                 ]
ICCID_PREFIXES = ["898600", "898601", "898602",  # China
                  "899210",  # UK
                  "899110",  # US
                  "899340",  # India
                  "899801",  # Japan
                  ]

PHONE_PREFIXES = ["138", "139", "150", "152", "188",  # China Mobile
                  "130", "131", "186",  # China Unicom
                  "133", "153", "189",  # China Telecom
                  "155", "156", "157", "166"]


# ------------------------------------------------------------------ generators
def _luhn_digit(partial: str) -> str:
    """Append a Luhn check digit so the IMEI is valid (15 digits)."""
    digits = [int(c) for c in partial]
    total = 0
    for i, d in enumerate(reversed(digits)):
        d = d * 2 if i % 2 == 0 else d
        if d > 9:
            d -= 9
        total += d
    return str((10 - total % 10) % 10)


def _rand_digits(n: int, rng) -> str:
    return "".join(rng.choice(string.digits) for _ in range(n))


def _rand_hex(n: int, rng) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def generate_profile(vendor: str | None = None, seed: int | None = None,
                     cpu: int = 4, memory: int = 1024,
                     resolution: str = "1280,720,240") -> DeviceProfile:
    """Build a fresh, realistic, unique device profile."""
    rng = random.Random(seed) if seed is not None else random

    if vendor and vendor.lower() in VENDORS:
        v = VENDORS[vendor.lower()]
        manufacturer = v["manufacturer"]
        model = rng.choice(v["models"])
    else:
        v = rng.choice(list(VENDORS.values()))
        manufacturer = v["manufacturer"]
        model = rng.choice(v["models"])

    imsi_prefix = rng.choice(IMSI_PREFIXES)
    iccid_prefix = rng.choice(ICCID_PREFIXES)

    partial = "86" + _rand_digits(12, rng)
    imei = partial + _luhn_digit(partial)
    imsi = imsi_prefix + _rand_digits(15 - len(imsi_prefix), rng)
    simserial = iccid_prefix + _rand_digits(19 - len(iccid_prefix), rng)
    androidid = _rand_hex(16, rng)
    mac = "".join(_rand_hex(2, rng) for _ in range(6)).upper()
    pnumber = rng.choice(PHONE_PREFIXES) + _rand_digits(8, rng)

    return DeviceProfile(
        imei=imei, imsi=imsi, simserial=simserial, androidid=androidid,
        mac=mac, pnumber=pnumber, manufacturer=manufacturer, model=model,
        cpu=cpu, memory=memory, resolution=resolution,
    )


# ------------------------------------------------------------------ apply
def apply_profile(console: LdConsole, name: str | None = None,
                  index: int | None = None, profile: DeviceProfile | None = None,
                  *, vendor: str | None = None, seed: int | None = None,
                  cpu: int | None = None, memory: int | None = None,
                  resolution: str | None = None,
                  root: bool = False, fast: bool = True,
                  light: bool = True, audio_off: bool = True) -> DeviceProfile:
    """Generate (if not given) and write a profile to an instance.

    Returns the profile that was applied. ``fast``/``light`` tune the global
    emulator performance knobs.
    """
    if profile is None:
        profile = generate_profile(
            vendor=vendor, seed=seed,
            cpu=cpu if cpu is not None else 4,
            memory=memory if memory is not None else (768 if light else 1024),
            resolution=resolution or ("960,540,240" if light else "1280,720,240"),
        )

    res = console.modify(
        name=name, index=index,
        cpu=profile.cpu, memory=profile.memory, resolution=profile.resolution,
        manufacturer=profile.manufacturer, model=profile.model,
        pnumber=profile.pnumber, imei=profile.imei, imsi=profile.imsi,
        simserial=profile.simserial, androidid=profile.androidid,
        mac=profile.mac,
        autorotate=0 if light else None,
        lockwindow=1 if light else None,
        root=1 if root else None,
    )
    if not res.ok:
        raise RuntimeError(f"apply profile failed: {res.text or res.stderr}")

    if fast or light or audio_off:
        console.global_setting(
            fps=24 if fast else None,
            audio=0 if audio_off else None,
            fastplay=1 if fast else None,
            cleanmode=1 if light else None,
        )
    return profile


def as_dict(profile: DeviceProfile) -> dict:
    return asdict(profile)
