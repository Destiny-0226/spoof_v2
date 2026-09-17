# SMBIOS Contract Implementation Plan

**Goal:** Make the four-stage SMBIOS stream structurally validated and consistent with the fixed Q35/OVMF guest model.

**Architecture:** Retain captured platform identity separately from virtual runtime capabilities. Share the stream builder and independent structural validation across generation, QEMU, OVMF and apply. Reject stale artifacts rather than rewriting them.

**Tech Stack:** Python standard library, SMBIOS 3.5, QEMU pc-q35-11.0, EDK2 OVMF X64.

## Approved design

- Validate every emitted table, its strings, handles, lengths, reserved bits and references; compare the complete stream with the declared profile.
- Extend Type 17 with DRAM/volatile capacity information. Keep unknown SPD identities, rank, rated speed and electrical measurements unknown rather than deriving them from a brand name or host DIMM.
- Preserve XML DIMM sizing and slot constraints. Host configured speed remains a declared virtual profile value, not a measurement of guest memory performance.
- Use XML CPU counts. Until guest cache topology is measured, omit Type 7 and use the SMBIOS 2.3+ FFFF unknown-cache sentinel; do not claim the host's aggregate cache is the guest's cache.
- Bind Type 0 ROM size to an explicit 4 MiB OVMF build and verify raw CODE plus VARS size. Preserve firmware branding, not unsupported legacy capabilities or physical EC revision.
- Retain platform connector metadata, but clear inherited Type 9 runtime usage/BDF observations. Do not create additional unimplemented devices to increase table count.
- Keep the four phases; no automatic regeneration, firmware build, VM define or restart.

## Implementation and verification

1. Reproduce non-memory mutations accepted by schema 24 using `.remote/verify_full_smbios.py` against the remote baseline; keep artifacts byte-identical.
2. Extract stream construction to `smbios_contract.py`; add strict parsing, supported-layout validation and whole-stream/profile checks. Wire all four phases through the existing shared validation entry point.
3. Update `smbios_memory.py` and phase 01 for schema 25, modern memory fields, explicit unknown values and virtual platform policy. Update phase 03 to enforce 4 MiB firmware and phase 04's build revision requirement.
4. Extend private remote tests with independent structure decoding, all-table mutations, malformed strings/references, stale schema rejection, empty slots and multiple CPU sockets. Do not add a new test framework to this repository.
5. Review the diff, commit only task files, push and fast-forward the clean remote to the exact commit. Run regression tests there; preserve existing identity and build artifacts.
6. Document the tested commit and remaining guest runtime checks. A pure Python test does not prove CPUID, firmware entry-point or guest WMI behavior.

## Reference

DMTF DSP0134 3.8.0, particularly sections 7.1, 7.5 and 7.18: https://www.dmtf.org/sites/default/files/standards/documents/DSP0134_3.8.0.pdf
