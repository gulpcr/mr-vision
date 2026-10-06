from __future__ import annotations

"""Minimum-necessary DICOM attributes for the viewer (checklist PRV-03, 164.502(b)).

The viewer gets exactly what it needs to find, order and display images: the study list
and series list are cut down to an allowlist, and every other DICOM answer the viewer
reads (WADO-RS metadata, instance search, retrieved files) loses the patient demographics
it never shows. The PACS keeps the complete record; this only limits what leaves it for a
display. Tags are DICOM JSON keys (group+element hex, no comma).
"""

# QIDO-RS study search (OHIF study list): identity as MRN, date, description, counts.
STUDY_QIDO_KEEP = frozenset({
    "00080005",  # SpecificCharacterSet
    "00080020",  # StudyDate
    "00080030",  # StudyTime
    "00080050",  # AccessionNumber
    "00080056",  # InstanceAvailability
    "00080061",  # ModalitiesInStudy
    "00080062",  # SOPClassesInStudy
    "00080201",  # TimezoneOffsetFromUTC
    "00081030",  # StudyDescription
    "00081190",  # RetrieveURL
    "00100010",  # PatientName (the MRN while DISPLAY_PATIENT_NAMES is off)
    "00100020",  # PatientID (MRN)
    "00100040",  # PatientSex
    "0020000D",  # StudyInstanceUID
    "00200010",  # StudyID
    "00201206",  # NumberOfStudyRelatedSeries
    "00201208",  # NumberOfStudyRelatedInstances
})

# QIDO-RS series search (OHIF display sets / thumbnails).
SERIES_QIDO_KEEP = frozenset({
    "00080005",  # SpecificCharacterSet
    "00080021",  # SeriesDate
    "00080031",  # SeriesTime
    "00080056",  # InstanceAvailability
    "00080060",  # Modality
    "0008103E",  # SeriesDescription
    "00081190",  # RetrieveURL
    "00180015",  # BodyPartExamined
    "0020000D",  # StudyInstanceUID
    "0020000E",  # SeriesInstanceUID
    "00200011",  # SeriesNumber
    "00201209",  # NumberOfSeriesRelatedInstances
})

# Removed from metadata, instance search and retrieved files: demographics and contact
# details the viewer never displays. (Image, geometry, acquisition and clinical-indication
# attributes stay - the radiologist needs them.)
VIEWER_EXCLUDED_TAGS = frozenset({
    "00080081",  # InstitutionAddress
    "00080092",  # ReferringPhysicianAddress
    "00080094",  # ReferringPhysicianTelephoneNumbers
    "00100030",  # PatientBirthDate
    "00100032",  # PatientBirthTime
    "00100050",  # PatientInsurancePlanCodeSequence
    "00101000",  # OtherPatientIDs
    "00101001",  # OtherPatientNames
    "00101002",  # OtherPatientIDsSequence
    "00101005",  # PatientBirthName
    "00101040",  # PatientAddress
    "00101050",  # InsurancePlanIdentification
    "00101060",  # PatientMotherBirthName
    "00101080",  # MilitaryRank
    "00101081",  # BranchOfService
    "00101090",  # MedicalRecordLocator
    "00102150",  # CountryOfResidence
    "00102152",  # RegionOfResidence
    "00102154",  # PatientTelephoneNumbers
    "00102155",  # PatientTelecomInformation
    "00102160",  # EthnicGroup
    "00102180",  # Occupation
    "001021F0",  # PatientReligiousPreference
    "00102297",  # ResponsiblePerson
    "00102299",  # ResponsibleOrganization
    "00104000",  # PatientComments
    "00380010",  # AdmissionID
    "00380400",  # PatientInstitutionResidence
})
