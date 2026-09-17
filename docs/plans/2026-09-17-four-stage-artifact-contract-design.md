# Four-Stage Artifact Contract Design

## Goal

Keep exactly the four executable Python stages. Phase 01 owns SMBIOS generation and deep semantic validation; phases 02-04 consume immutable artifacts instead of importing shared Python modules or reconstructing SMBIOS.

## Artifact Contract

Phase 01 writes `identity-hardware.json` and `smbios.bin`. Schema 27 adds an artifact contract containing the canonical identity payload SHA-256, SMBIOS path, SHA-256, byte length, entry-point fields, table count, final Type 127 handle and runtime CPU-ID policy. Phase 01 validates the complete stream before either artifact is atomically published.

Phases 02 and 03 verify schema 27, the canonical identity payload hash and the SMBIOS metadata/hash before building. They consume only their policy sections from the identity file. Phase 04 performs the same artifact checks, verifies QEMU/OVMF build metadata against the exact identity file, and separately checks live XML memory and CPU topology against the phase 01 baseline.

## Compatibility

Existing schema 26 artifacts are rejected and must be regenerated. SMBIOS table semantics and bytes remain governed by the existing contract, including the `0xFEFF` Type 127 handle and QEMU runtime replacement of the Type 4 Processor ID placeholder.

## Files

The repository retains only `01_generate_identity.py`, `02_patch_qemu.py`, `03_patch_ovmf.py`, and `04_apply_spoof.py` as Python sources. `smbios_memory.py` and `smbios_contract.py` are removed after their generation and validation logic is moved into phase 01.
