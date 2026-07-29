"""HL7 v2 domain model — pure dataclasses + enums, stdlib only.

These types cross the layer boundary between the infrastructure MLLP/parsing
adapters and the application ingest service. Per the domain-layer rule they import
nothing from application/infrastructure/interface and no framework code.

Note on PHI: ``ParsedPatient`` carries ``patient_name`` and ``birth_date`` only
transiently — the mapper derives an age band and discards them. They must never be
persisted when phi_deidentify_enabled is in effect (see the mapping service).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class HL7MessageType(str, enum.Enum):
    """Supported inbound/outbound HL7 v2 trigger events (MSH-9)."""

    ADT_A01 = "ADT^A01"   # admit / visit notification
    ADT_A04 = "ADT^A04"   # register a patient
    ADT_A08 = "ADT^A08"   # update patient information
    ADT_A40 = "ADT^A40"   # merge patient — patient identifier list
    ORM_O01 = "ORM^O01"   # general order message
    OMG_O19 = "OMG^O19"   # general clinical order (imaging)
    ORU_R01 = "ORU^R01"   # observation result (outbound)
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_msh9(cls, value: str) -> "HL7MessageType":
        """Map a raw MSH-9 value (``ADT^A01`` or ``ADT^A01^ADT_A01``) to a member,
        returning UNKNOWN for anything unsupported rather than raising."""
        if not value:
            return cls.UNKNOWN
        parts = value.split("^")
        key = "^".join(parts[:2]) if len(parts) >= 2 else parts[0]
        try:
            return cls(key.upper())
        except ValueError:
            return cls.UNKNOWN


class AckCode(str, enum.Enum):
    """HL7 acknowledgment codes (MSA-1)."""

    AA = "AA"   # application accept
    AE = "AE"   # application error
    AR = "AR"   # application reject


@dataclass
class ParsedPatient:
    """Demographics extracted from a PID segment.

    ``patient_name`` / ``birth_date`` are PHI held only long enough for the mapper
    to derive ``age_band``; they are never written to the de-identified patients table.
    """

    mrn: str
    sex: str | None = None            # normalised to female|male|other by the mapper
    birth_date: datetime | None = None
    patient_name: str | None = None
    tenant_id: str = "default"


@dataclass
class ParsedOrder:
    """An imaging order extracted from ORC/OBR segments."""

    mrn: str
    placer_order_number: str | None = None
    filler_order_number: str | None = None
    universal_service_id: str | None = None   # OBR-4 (procedure code / description)
    modality: str | None = None
    body_part: str | None = None
    region_profile: str | None = None
    priority: str = "routine"                  # routine|stat
    reason_for_study: str | None = None
    referrer: str | None = None
    study_instance_uid: str | None = None
    scheduled_dt: datetime | None = None
    tenant_id: str = "default"


@dataclass
class ParsedMessage:
    """The structured result of parsing one raw HL7 message."""

    message_type: HL7MessageType
    message_control_id: str
    sending_facility: str | None = None
    version: str | None = None
    patient: ParsedPatient | None = None
    order: ParsedOrder | None = None


@dataclass
class ProcessResult:
    """Outcome of handling one inbound message, carrying what the listener needs to
    build the MLLP ACK and what the message log needs to persist."""

    ack_code: AckCode
    message_control_id: str
    message_type: HL7MessageType = HL7MessageType.UNKNOWN
    error_detail: str | None = None
    patient_ref: str | None = None
    order_id: str | None = None
    study_instance_uid: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.ack_code is AckCode.AA
