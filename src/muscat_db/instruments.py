from dataclasses import dataclass, field


@dataclass(frozen=True)
class InstrumentConfig:
    name: str
    nccd: int
    data_dir: str
    prefix: str
    ep_names: list[str] | None = None
    keys: list[str] = field(default_factory=list)
    csv_header: str = ""
    has_pa: bool = False
    focus_label: str = "FOCUS (mm)"
    airmass_key: str = "SECZ"
    use_alt_ut_key: bool = False


MUSCAT = InstrumentConfig(
    name="muscat",
    nccd=3,
    data_dir="/data/MuSCAT",
    prefix="MSCT",
    keys=["OBJECT", "MJD-STRT", "EXP-STRT", "EXPTIME", "SPDTAB", "FILTER", "RA", "DEC", "SECZ", "FOC-VAL", "INST-PA"],
    csv_header="FRAME,OBJECT,JD-STRT,UT-STRT,EXPTIME (s),READ_MODE,FILTER,RA,DEC,SECZ,FOCUS (mm),PA (deg)",
    has_pa=True,
    focus_label="FOCUS (mm)",
    airmass_key="SECZ",
    use_alt_ut_key=False,
)

MUSCAT2 = InstrumentConfig(
    name="muscat2",
    nccd=4,
    data_dir="/data/MuSCAT2",
    prefix="MCT2",
    keys=["OBJECT", "MJD-STRT", "EXP-STRT", "EXPTIME", "SPDTAB", "FILTER", "RA", "DEC", "AIRMASS", "FOC-VAL", "INST-PA"],
    csv_header="FRAME,OBJECT,JD-STRT,UT-STRT,EXPTIME (s),READ_MODE,FILTER,RA,DEC,AIRMASS,FOCUS (um),PA (deg)",
    has_pa=True,
    focus_label="FOCUS (um)",
    airmass_key="AIRMASS",
    use_alt_ut_key=False,
)

MUSCAT3 = InstrumentConfig(
    name="muscat3",
    nccd=4,
    data_dir="/data/MuSCAT3",
    prefix="ogg2m001-",
    ep_names=["ep02", "ep03", "ep04", "ep05"],
    keys=["OBJECT", "MJD-OBS", "UTSTART", "EXPTIME", "CONFMODE", "FILTER", "RA", "DEC", "AIRMASS", "FOCPOSN"],
    csv_header="FRAME,OBJECT,JD-STRT,UT-STRT,EXPTIME (s),READ_MODE,FILTER,RA,DEC,AIRMASS,FOCUS (mm)",
    has_pa=False,
    focus_label="FOCUS (mm)",
    airmass_key="AIRMASS",
    use_alt_ut_key=True,
)

_MUSCAT4_EP_OLD = ["ep06", "ep07", "ep08", "ep10"]
_MUSCAT4_EP_NEW = ["ep06", "ep07", "ep08", "ep09"]

MUSCAT4 = InstrumentConfig(
    name="muscat4",
    nccd=4,
    data_dir="/data/MuSCAT4",
    prefix="coj2m002-",
    ep_names=_MUSCAT4_EP_NEW,
    keys=["OBJECT", "MJD-OBS", "UTSTART", "EXPTIME", "CONFMODE", "FILTER", "RA", "DEC", "AIRMASS", "FOCPOSN"],
    csv_header="FRAME,OBJECT,JD-STRT,UT-STRT,EXPTIME (s),READ_MODE,FILTER,RA,DEC,AIRMASS,FOCUS (mm)",
    has_pa=False,
    focus_label="FOCUS (mm)",
    airmass_key="AIRMASS",
    use_alt_ut_key=True,
)

SINISTRO = InstrumentConfig(
    name="sinistro",
    nccd=1,
    data_dir="/data/Sinistro",
    prefix="",
    ep_names=[""],
    keys=["OBJECT", "MJD-OBS", "UTSTART", "EXPTIME", "CONFMODE", "FILTER", "RA", "DEC", "AIRMASS", "FOCPOSN"],
    csv_header="FRAME,OBJECT,JD-STRT,UT-STRT,EXPTIME (s),READ_MODE,FILTER,RA,DEC,AIRMASS,FOCUS (mm)",
    has_pa=False,
    focus_label="FOCUS (mm)",
    airmass_key="AIRMASS",
    use_alt_ut_key=True,
)

INSTRUMENTS: dict[str, InstrumentConfig] = {
    "muscat": MUSCAT,
    "muscat2": MUSCAT2,
    "muscat3": MUSCAT3,
    "muscat4": MUSCAT4,
    "sinistro": SINISTRO,
}


def get_instrument(name: str) -> InstrumentConfig:
    inst = INSTRUMENTS.get(name)
    if inst is None:
        msg = f"Unknown instrument '{name}'. Choose from: {', '.join(INSTRUMENTS)}"
        raise ValueError(msg)
    return inst


OBSLOG_BASE = "/ut3/muscat/obslog"
