# Four-Stage Artifact Contract Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make phase 01 the sole owner of SMBIOS logic and retain only the four stage Python files.

**Architecture:** Inline the current SMBIOS memory and full-stream implementation into phase 01. Publish a schema 27 artifact contract that later stages verify using hashes and fixed metadata instead of importing or reconstructing SMBIOS.

**Tech Stack:** Python standard library, JSON, SHA-256, libvirt XML, QEMU, EDK2/OVMF.

---

### Task 1: Establish Structural Regression

**Files:**
- Test: `.remote/verify_four_stage_artifacts.py`

1. Assert the tracked Python file set contains only phases 01-04.
2. Assert no stage imports `smbios_memory` or `smbios_contract`.
3. Run against the current baseline and observe the expected failure.

### Task 2: Move SMBIOS Ownership Into Phase 01

**Files:**
- Modify: `01_generate_identity.py`
- Delete: `smbios_memory.py`
- Delete: `smbios_contract.py`

1. Inline the existing memory and complete-stream generation/validation functions without changing table bytes.
2. Remove the shared-module imports and circular validation import.
3. Raise the identity schema to 27.

### Task 3: Publish and Consume the Artifact Contract

**Files:**
- Modify: `01_generate_identity.py`
- Modify: `02_patch_qemu.py`
- Modify: `03_patch_ovmf.py`
- Modify: `04_apply_spoof.py`

1. Store canonical identity payload and SMBIOS metadata/hashes in the generated identity.
2. Add small stage-local contract validators to phases 02-04.
3. Keep phase 04 live XML memory and topology checks local.
4. Bump QEMU/OVMF patch revisions because their build inputs and validation contract change.

### Task 4: Update Documentation and Regression Harnesses

**Files:**
- Modify: `README.md`
- Modify: `docs/plans/2026-09-17-smbios-contract.md`
- Modify: private `.remote` regression scripts

1. Document phase 01 ownership and schema 27.
2. Update private fixtures to load phase 01 for SMBIOS tests.
3. Verify old schema rejection, artifact tamper rejection and exact four-file structure.

### Task 5: Verify and Deploy Through Git

1. Run local structural tests, Python compilation and diff checks.
2. Commit only task files and push `main`.
3. Fast-forward the clean remote to the exact commit.
4. Run full SMBIOS, memory, compile-order and artifact-contract regressions without regenerating the current identity.
