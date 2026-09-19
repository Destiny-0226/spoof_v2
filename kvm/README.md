# OVO KVM patch installer

```bash
./kvm.sh
```

With no arguments the script asks:

```text
1) Install KVM patch
2) Restore backup
q) Quit
```

Choice 1 runs the normal patch build and install. Choice 2 lists
`/var/backups/ovo-kvm` (newest first) and asks which directory to restore.
`0` cancels restore. Reboot after either.

Commands still work without the menu:

```bash
./kvm.sh install --yes
./kvm.sh install --build-only
./kvm.sh restore
./kvm.sh restore 7.1.3-1-intel-20260830-141500
```

## Layout

```text
kvm.sh                 # install / restore
kvm/patches/           # 7.1.3-1.intel.patch, 7.2.2-1.amd.patch, ...
kvm/ovo-modfinal.mk    # required: final .ko link after custom modpost
/var/backups/ovo-kvm   # <package-version>-<intel|amd>-<timestamp>/
```

Backup directories look like
`/var/backups/ovo-kvm/7.1.3-1-intel-20260830-141500/` and contain
`kvm.ko.zst`, `kvm-intel.ko.zst` or `kvm-amd.ko.zst`, `SHA256SUMS`, and
`METADATA`. The repo does not store module backups.

If no patch matches the running kernel package and CPU vendor, `install`
prints the expected filename and exits.

## ovo-modfinal.mk

This file is still required. The installer does a custom `modpost` against the
installed kernel ABI, then invokes the kernel's `Makefile.modfinal` through
`ovo-modfinal.mk` to produce `kvm.ko` and `kvm-intel.ko` / `kvm-amd.ko`.
Do not delete it.

## Patch files

| File | Vendor | Package |
| --- | --- | --- |
| `6.19.14-ovo.intel.patch` | Intel | manually installed 6.19.14-ovo; official Nika-based release |
| `6.19.rc6-1.intel.patch` | Intel | 6.19.rc6-1 (`linux-cachyos-rc-lto`) |
| `7.1.3-1.intel.patch` | Intel | 7.1.3-1; complete 6.19.14 OVO port |
| `7.1.3-1.intel.v2.patch` | Intel | 7.1.3-1; mediated-vPMU revision, with hypercall interception fix |
| `7.1.3-1.intel.v2-timing.patch` | Intel | 7.1.3-1; experimental v2 plus standard CPUID re-entry fastpath |
| `7.1.3-1.intel.v2-timing2.patch` | Intel | 7.1.3-1; timing fastpath plus VMX-local completion and CPUID.0 cache |
| `7.1.3-1.intel.v2-timing3.patch` | Intel | 7.1.3-1; generic static-CPUID cache plus guarded VMX inner re-entry |
| `7.1.3-1.intel.v2-timing4.patch` | Intel | 7.1.3-1; timing3 plus opt-in P-core RFDS-only VERW experiment |
| `7.1.3-1.intel.v2-timing5.patch` | Intel | 7.1.3-1; timing4 plus opt-in pre-spill direct-map CPUID path |
| `7.1.3-1.intel.v2-timing-baseline.patch` | Intel | 7.1.3-1; clean timing baseline (timing5, without relaxed/profile experiments) |
| `7.1.3-1.intel.v2-timing7-leafdiag.patch` | Intel | 7.1.3-1; baseline plus read-only CPUID(1)/CPUID(7) miss counters |
| `7.1.3-1.intel.v2-timing7-leafdiag2.patch` | Intel | 7.1.3-1; adds CPUID(0xD)/CPUID(0x80000007) miss counters |
| `7.1.3-1.intel.v2-timing7-leafdiag3.patch` | Intel | 7.1.3-1; records the last unclassified miss leaf |
| `7.1.3-1.intel.v2-timing8-dynamic-cache.patch` | Intel | 7.1.3-1; generation-safe cache for CPUID(1) and CPUID(0xD) |
| `7.1.3-1.intel.v2-timing9-allcpuid.patch` | Intel | 7.1.3-1; canonical generation-safe cache for all configured CPUID leaves |
| `7.1.3-1.intel.v2-timing10-synthetic-cache.patch` | Intel | 7.1.3-1; timing9 plus KVM-generated cache entries for common synthetic probes |
| `7.1.3-1.intel.v2-timing11-profile.patch` | Intel | 7.1.3-1; frozen timing10 plus opt-in sampled inner-path profiling |
| `7.1.3-1.intel.v2-timing12-transitiondiag.patch` | Intel | 7.1.3-1; timing11 plus sampled VMRESUME-to-next-CPUID-VMEXIT diagnostics |
| `7.1.3-1.intel.v2-timing13-default-tsc-scaling.patch` | Intel | 7.1.3-1; rejected identity TSC-scaling experiment (archive only) |
| `7.1.3-1.intel.v2-timing14-state-load-audit.patch` | Intel | 7.1.3-1; timing13 plus sampled dedicated VM-entry/exit state-load audit |
| `7.1.3-1.intel.v2-timing15-lazy-perf.patch` | Intel | 7.1.3-1; rejected lazy PERF_GLOBAL_CTRL experiment (archive only) |
| `7.1.3-1.intel.v2-timing16-deferred-reg-sync.patch` | Intel | 7.1.3-1; rejected deferred GPR-sync experiment (archive only) |
| `7.1.3-1.intel.v2-timing17-mru-cache.patch` | Intel | 7.1.3-1; clean baseline plus generation-safe per-vCPU CPUID MRU lookup |
| `7.1.3-1.intel.v2-timing17-msr.patch` | Intel | 7.1.3-1; timing17 plus CPUID-gated host RDMSR (writes never hit host) |
| `7.1.3-1.intel.v2-msr-silicon.patch` | Intel | 7.1.3-1; timing17 plus silicon-default guest MSR contract |
| `7.1.3-1.intel.v2-timing6-profile.patch` | Intel | 7.1.3-1; timing5 plus opt-in sampled cycle profiling |
| `7.1.3-1.amd.patch` | AMD | 7.1.3-1 |
| `7.2.2-1.amd.patch` | AMD | 7.2.2-1 |

A later `7.2.2-2` package needs its own patch file. Use `--patch FILE` only
while testing. Each patch must apply directly to the matching official source
release.

The manually installed `6.19.14-ovo` kernel is also supported even though its
module tree is not owned by pacman. Its prepared build tree must remain at
`/usr/lib/modules/6.19.14-ovo/build`. The installer automatically selects
`6.19.14-ovo.intel.patch`; no `--patch` override is needed:

```bash
./kvm.sh install --build-only --skip-deps --jobs 4 \
  --source-archive /home/lx/.cache/ovo-kvm/downloads/linux-6.19.14.tar.gz \
  --work-dir .work/nika-fixed
```

## Intel v2 silicon-default MSRs (2026-09-19)

`7.1.3-1.intel.v2-msr-silicon.patch` is timing17-msr with the guest MSR
default inverted. vCPU-state MSRs (TSC, APIC, EFER, MTRR, SPEC_CTRL,
vPMU, MCE) stay on KVM. KVM/HV PV MSRs `#GP` unless guest CPUID has
hypervisor. Every other guest RDMSR/WRMSR follows host `rdmsrq_safe`:
missing => #GP, present => host value or per-vCPU overlay. KVM fake
zeros/constants (`PERF_STATUS` 4x, RAPL=0, `PLATFORM_ID`=0,
`BBL_CR_CTL3`, Intel guests seeing AMD `K7_CLK_CTL`) no longer win on
the guest path. Writes never `wrmsr` the host.
`IA32_FEAT_CTL` (0x3A) reads as LOCKED with VMX bits taken from guest
CPUID (not the host). `IA32_DEBUG_INTERFACE` (0xC80) reads LOCKED.
`IA32_UCODE_REV` (0x8B) always reads host silicon; guest `WRMSR` is
discarded. Guest-visible `MISC_ENABLE` keeps host identity bits
(Fast-Strings, TM1, EMON, BTS/PEBS unavailable, EIST, xTPR disable).
Guest reads of `POWER_CTL`, `ARCH_CAPABILITIES`, and
`PERF_CAPABILITIES` return host silicon. LBR/DS_AREA exist if the host
has them but return 0/overlay, not host RIP. Overlay is 256 slots.
`ignore_msrs` stays 0. HFI MSRs stay hidden. MTRR_CAP and MCG_CAP stay
on KVM (they describe what KVM implements, not host bank/range counts).

Close VMs, then:

```bash
./kvm.sh install --yes \
  --patch kvm/patches/7.1.3-1.intel.v2-msr-silicon.patch
```

Reboot afterwards. `01`–`04` stay unchanged.

## Intel v2 timing17 + host MSR reads (2026-09-17)

`7.1.3-1.intel.v2-timing17-msr.patch` is timing17 plus generic MSR/CPUID
handling. It does not edit the timing17 file. Unimplemented MSRs follow the
host `rdmsrq_safe` oracle (#GP). Guest CPUID only hides **named** Intel VT
and AMD-V MSRs when those features are off. Identity MSRs that KVM used to
fake are read from the current pCPU and never fall through to KVM constants.
Guest CPUID host-fill is an allowlist: `0x06`, `0x15`, `0x16`, `0x1A`. `0x06`
keeps Turbo and HWP, and clears HFI (EAX.19) and ITD (EAX.23) until a
guest HFI table exists. Advertising HFI made Win10 `WRMSR 0x17D0` then
`SYSTEM_THREAD_EXCEPTION_NOT_HANDLED` when the write #GP'd.

MSR writes never hit host silicon. RO status (PERF_STATUS, RAPL energy,
turbo limits, HWP_CAPABILITIES) still #GP. Programmable identity MSRs
(HWP_REQUEST, PERF_CTL, thermal interrupts, and any other host-existing
MSR that is not RO) succeed into a per-vCPU overlay; RDMSR returns the
overlay then the host value. HFI MSRs `0x17D0–0x17DA` stay hidden.
W1C status (THERM_STATUS, HWP_STATUS) accepts the write and keeps
host reads. Topology, XML feature masks, vPMU, XSAVE, and
`0x40000000–0x4fffffff` stay on the KVM/QEMU table.

Close VMs, then:

```bash
./kvm.sh install --yes \
  --patch kvm/patches/7.1.3-1.intel.v2-timing17-msr.patch
```

Reboot afterwards. `01`–`04` stay unchanged.

## Intel v2 timing5 pre-spill CPUID experiment (2026-09-11)

`7.1.3-1.intel.v2-timing5.patch` is a complete patch against the official
7.1.3-1 source. Its validated SHA256 is
`9b15ad9800815f95188fa422422d4241ad0c1c570db0469bfaf82496caf7ec3f`.

Timing4 measurements isolated about 22--23 TSC cycles of RFDS-only `VERW`
cost. Timing5 targets the larger remaining software component. It checks the
non-indexed and indexed direct-map home slots before spilling all guest GPRs.
On a hit, only CPUID's four architectural output registers are used as scratch;
all other guest registers stay resident until VMRESUME. The canonical
RAX/RBX/RCX/RDX array is still synchronized for VMRESUME failure handling.

The early path repeats timing3's exit-reason, event-vectoring, interrupt-shadow
and TF validation. A collision or any failed check falls back to the complete
timing4 path. It is generic across cacheable static leaves rather than special
casing leaf 0. The new switch defaults to `N`.

Install explicitly, close running VMs first, and reboot afterwards:

```bash
./kvm.sh install --yes \
  --patch kvm/patches/7.1.3-1.intel.v2-timing5.patch \
  --work-dir .work/v2-timing5 --jobs 8
```

For an A/B comparison, leave the timing4 RFDS switch enabled and change only:

```bash
# timing4-equivalent control
echo N | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_early

# timing5 pre-spill path
echo Y | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_early
```

`cpuid_inner_early_armed=1` confirms the assembly flag is armed. Direct-slot
hits should improve; collided entries and dynamic leaves remain control groups.
Run `python3 test_kvm_cpuid_timing5.py` for structural and lookup fallback
checks.

## Intel v2 clean timing baseline (2026-09-11)

`7.1.3-1.intel.v2-timing-baseline.patch` is the frozen control patch for the
next optimization round. It is byte-identical to the validated timing5 patch
(`9b15ad9800815f95188fa422422d4241ad0c1c570db0469bfaf82496caf7ec3f`). It
retains the CPUID cache, VMX-local completion, guarded inner fastpath,
pre-spill early path, and the separately gated P-core RFDS optimization.

The baseline deliberately does **not** include the timing6 sampled profiler or
the experimental `fast_cpuid_inner_relaxed` path. The normal IDT-vectoring,
guest-interruptibility, and guest-RFLAGS/TF VMCS checks therefore remain in the
fastpath. The timing6 patch is kept as a diagnostic artifact, not as the
control kernel.

Install this patch only when a clean control kernel is needed:

```bash
./kvm.sh install --yes \
  --patch kvm/patches/7.1.3-1.intel.v2-timing-baseline.patch \
  --work-dir .work/v2-timing-baseline --jobs 8
```

Run `python3 test_kvm_cpuid_timing5.py` for the structural checks. Keep
`fast_cpuid_inner_relaxed` and `fast_cpuid_inner_profile` disabled (or absent)
when recording baseline measurements.

## Intel v2 timing7 CPUID leaf diagnostics (2026-09-11)

`7.1.3-1.intel.v2-timing7-leafdiag.patch` is an observation-only derivative of
the frozen baseline. It does not change CPUID results, VMCS validation, cache
eligibility, or re-entry decisions. On an inner-cache miss it counts whether
the requested leaf was `0x1`, `0x7`, or another leaf, and exposes the counters
through per-vCPU debugfs files:

```text
cpuid_inner_miss_leaf1
cpuid_inner_miss_leaf7
cpuid_inner_miss_other
```

Its SHA256 is
`efc0f098fafbddd0573432a174e5a3621b54970ec9145c557585b10be2d53157`.
The patch was applied cleanly to the timing5 source overlay and the VMX
objects compiled successfully. It is diagnostic only; no new optimization is
included.

The first leaf-diagnostic run showed that vCPU2's `other` bucket dominated
(`140050` of `205503` misses), so guessing between leaf 1 and leaf 7 would be
premature. `7.1.3-1.intel.v2-timing7-leafdiag2.patch` keeps the same behavior
and additionally separates CPUID `0xD` and `0x80000007`; its SHA256 is
`c9067f4c5a5e03ab2a0903dae0d51a3129be4b8346c3577d1992d279d7c4a441`. Use it for the next single diagnostic run, without recording another
CSV.

The follow-up run showed that vCPU2's `other` bucket was still large, so
`7.1.3-1.intel.v2-timing7-leafdiag3.patch` adds one more read-only field,
`cpuid_inner_miss_other_last`, containing the most recent unclassified leaf.
This identifies the remaining hot leaf without another guess-and-rebuild
cycle. Its SHA256 is
`0fe67fc9b8d8d0d1400220b006fbe2945d347974b765d3768c7aeafa1da28722`.

## Intel v2 timing8 dynamic CPUID cache (2026-09-11)

`7.1.3-1.intel.v2-timing8-dynamic-cache.patch` is the first behavior-changing
optimization after the frozen baseline. It allows the existing guarded inner
fastpath to cache CPUID leaf `0x1` and the indexed `0xD` leaves. A generation
counter is incremented whenever KVM refreshes runtime CPUID bits; VMX rebuilds
the cache only when that generation changes. The normal VMCS safety checks,
debug/interrupt/SMM/nested fallbacks, and all other leaf handling remain
unchanged. CPUID(7), CPUID(0x80000007), and out-of-range control leaves remain
on the standard path in this first experiment.

Its SHA256 is
`467fa265ffbfb8f6302b208f8ea45add42c45a32db01e3ff11f5e27d9e895746`.
The patch applies cleanly to the timing5 source overlay and passes the
timing8 structural checks; use the normal `kvm.sh install` build to perform
the full kernel-module compile. Do not enable `relaxed` or the profiler when
measuring it.

## Intel v2 timing9 canonical all-CPUID cache (2026-09-12)

`7.1.3-1.intel.v2-timing9-allcpuid.patch` extends timing8 to every configured
CPUID leaf and indexed subleaf that is not marked stateful and does not mix
indexed and non-indexed entries. Cache values are generated through KVM's own
`kvm_cpuid()` path, so TSX, invariant-TSC, Xen, and other runtime adjustments
are preserved instead of copying raw userspace entries. The generation tag
invalidates the snapshot when KVM refreshes runtime CPUID state. Exceptional
VMCS, debug, interrupt, SMM, nested, PMU, and dynamic-state guards remain
unchanged; stateful or ambiguous entries still use the standard path.

Its SHA256 is
`de38a909939c1b714bdc49b539bb1f2de5c883c9fb9bfd087a31d7f72fd13280`.
The patch applies cleanly to the timing8 source overlay and passes the timing9
structural checks. The KVM C/assembly objects compile; a standalone partial
tree build stops later at modpost because the full kernel `Module.symvers` is
not present. Build it with the normal `kvm.sh install` flow before measuring.
Do not enable `relaxed` or the profiler during the comparison.

Build/install it as the next cumulative experiment:

```bash
./kvm.sh install --yes \
  --patch kvm/patches/7.1.3-1.intel.v2-timing9-allcpuid.patch \
  --work-dir .work/v2-timing9-allcpuid --jobs 8
```

## Intel v2 timing10 synthetic CPUID probe cache (2026-09-12)

`7.1.3-1.intel.v2-timing10-synthetic-cache.patch` keeps timing9's
generation-safe cache and adds a small set of observed architectural and
out-of-range probes: CPUID `0xB/0xFF`, `0x1F`, hypervisor leaves
`0x40000000`/`0x40000100`, the highest basic/extended leaves, `0xC0000000`,
and `0xFFFFFFFF`. Each value is obtained by calling KVM's normal
`kvm_cpuid()` oracle during cache construction, so Intel max-leaf redirect,
AMD zeroing, indexed topology semantics, and dynamic adjustments are not
reimplemented or guessed. Existing exact configured entries win; synthetic
entries only fill otherwise-missing keys.

This is deliberately bounded rather than an unsafe arbitrary-function cache.
After measuring these common probes, the next stage can add a learned fallback
for repeated unknown functions with an explicit validity/eviction policy.
The patch applies cleanly to the timing8 source overlay and the VMX C/assembly
objects compile; a partial-tree build may still stop at modpost without the
full kernel `Module.symvers`.

The controlled run with `fast_cpuid_inner_early=Y` and
`fast_cpuid_inner_skip_rfds_pcore=Y` reduced the four previous outliers from
about 5.4--5.5k software ticks to 2.09--2.12k. All 15 measured rows now stay
within the same band; the maximum p10 ratio fell from 6.48 to 2.38. The
inner-fastpath counters reported zero blockers and only 3--33 cache misses per
vCPU, confirming that the synthetic entries are being used rather than merely
being present in the source.

Its SHA256 is
`ae0adf89779b9f23ed357767be4520f7261954742d5db043189210fa950cdfb2`.
Build/install it with:

```bash
./kvm.sh install --yes \
  --patch kvm/patches/7.1.3-1.intel.v2-timing10-synthetic-cache.patch \
  --work-dir .work/v2-timing10-synthetic-cache --jobs 8
```

## Intel v2 timing11 cumulative stage profiler (2026-09-12)

`7.1.3-1.intel.v2-timing11-profile.patch` keeps timing10 behavior unchanged
when profiling is disabled. Its opt-in, one-in-1024 sampled instrumentation
records cumulative TSC endpoints for validation, cache lookup, guest-RIP
VMREAD/VMWRITE, and the pre-VMRESUME preparation. The existing diagnostic
script converts those endpoints into per-stage averages. It deliberately does
not restore the retired `relaxed` path or remove any VMCS safety checks.

The cumulative patch applies and reverses cleanly against the unmodified
7.1.3-1 source. Both `kvm.o` and `kvm-intel.o`, including regenerated assembly
offsets and `vmenter.S`, compile successfully. Its SHA256 is
`48cd0c960de1b652c936cb0c1479f1053762791817b564563fb593fea72e6aca`.

The vCPU2 run produced 162 samples. Its cumulative endpoints decode to
86.15 cycles for validation, 46.60 for lookup, 43.96 for RIP/VMWRITE, and
36.62 for pre-resume work, or 213.33 profiled host cycles in total. The guest
measured about 702 TSC cycles for CPUID, so roughly 489 cycles lie outside the
instrumented assembly interval. That remainder is primarily hardware
VM-exit/VM-entry plus a small amount of guest-side measurement boundary work;
it is not evidence that another CPUID leaf lookup remains slow. All 15 leaf
medians remained in the same 2104--2123 software-tick band.

```bash
./kvm.sh install --yes \
  --patch kvm/patches/7.1.3-1.intel.v2-timing11-profile.patch \
  --work-dir .work/v2-timing11-profile --jobs 8
```

## Intel v2 timing12 transition diagnostics (2026-09-12)

`7.1.3-1.intel.v2-timing12-transitiondiag.patch` preserves timing10's CPUID
results and timing11's internal-stage profiler. When the profiler samples a
successful direct CPUID hit, timing12 also records a TSC immediately before
VMRESUME and closes the interval at the beginning of the next VMEXIT. The
sample is committed only after that next exit is confirmed as CPUID. Minimum,
average, and maximum intervals are exported separately, so interrupt outliers
do not hide the lower-bound transition cost.

The same opt-in switch takes a one-time per-vCPU snapshot of VM-entry/exit MSR
load/store counts and the VM-entry, VM-exit, primary-execution, and
secondary-execution controls. No control is changed. The retired `relaxed`
path remains absent and all VMCS safety checks remain active. Profiling stays
off by default.

The interval includes the small guest burst-loop boundary and profiler
fringes; it is not a claim to measure Intel microcode alone. It does exclude
the CPUID cache lookup and RIP update already measured by timing11. The
cumulative patch applies and reverses cleanly against unmodified 7.1.3-1
sources. The full focused build, installed-kernel ABI modpost, final `.ko`
link, BTF generation, compression, and staging all complete successfully; no
system module was changed by this build-only validation. Its SHA256 is
`a0b1eaca316a8842f4938573646452c7dca658179828c33c85ea7eafc46bac06`.

The pinned vCPU2 burst produced 203 complete transition samples. Its
VMRESUME-to-next-CPUID-VMEXIT interval was 612/645.00/2852 host TSC cycles
(minimum/average/maximum). The same vCPU's sampled internal stages were
89.13 cycles for validation, 41.21 for lookup, 36.13 for RIP/VMWRITE, and
30.59 for pre-resume work, or 197.06 cycles total. Thus the steady burst's
measured transition portion is about 76.6% of transition plus profiled inner
work. The minimum is the useful hardware-floor indicator; other vCPUs had few
samples and large interrupt/scheduling outliers.

Every vCPU reported zero VM-entry/exit MSR-list entries. The active control
snapshot was identical across vCPUs: VM-exit `0x502bffff`, VM-entry
`0x0010f1ff`, primary execution `0xb5a265fa`, and secondary execution
`0x061017eb`. This rules out MSR-list processing as the missing bulk cost and
shows that the remaining dominant interval is the hardware VM-entry/VM-exit
boundary plus the small guest burst-loop and profiler fringes. Do not add the
197.06-cycle sampled-handler figure directly to an unprofiled guest CPUID
measurement: the four LFENCE/RDTSC stage probes deliberately inflate sampled
handler executions.

The live vCPU2 debugfs ratio was `281474976710656` with 48 fractional bits,
exactly 1.0, while secondary control bit 25 (`TSC_SCALING`) remained enabled.
Both `enable_pmu` and `enable_mediated_pmu` were `Y`, explaining the active
PERF_GLOBAL_CTRL entry/exit controls. A semantics-preserving next experiment
is therefore to clear TSC scaling only for the default 1.0 multiplier and
measure the same burst. PERF_GLOBAL_CTRL switching must not be removed merely
because its VMCS MSR-list counts are zero; mediated PMU correctness depends on
the dedicated entry/exit controls when guest counters are active.

Install it from fish:

```fish
./kvm.sh install --yes \
  --patch kvm/patches/7.1.3-1.intel.v2-timing12-transitiondiag.patch \
  --work-dir .work/v2-timing12-transitiondiag --jobs 8
```

After rebooting and starting the VM, enable one-shot diagnostics on the host:

```fish
printf 'Y\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_early
printf 'Y\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_skip_rfds_pcore
printf 'Y\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_profile
```

Compile the updated `tools/cpuid_latency.cpp` in an x64 Native Tools Command
Prompt in the guest, then run exactly one pinned burst:

```bat
cl /nologo /std:c++17 /O2 /EHsc cpuid_latency.cpp /Fe:cpuid_latency.exe
cpuid_latency.exe --transition-burst 2 200000
```

Collect the host result and immediately disable profiling:

```fish
sudo bash kvm/tools/cpuid_inner_diag.sh | tee ~/桌面/ttest/timing12-transitiondiag.txt
printf 'N\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_profile
```

The decisive line is
`vmresume_to_cpuid_vmexit_min/avg/max`. Run
`python3 test_kvm_cpuid_timing12_transitiondiag.py` for structural checks.

## Intel v2 timing13 identity TSC-scaling elision (2026-09-12)

`7.1.3-1.intel.v2-timing13-default-tsc-scaling.patch` is a cumulative timing12
patch. The timing12 capture showed that vCPU2's TSC multiplier was exactly the
KVM identity value (`1 << 48`) even though secondary execution control bit 25,
`SECONDARY_EXEC_TSC_SCALING`, remained enabled. Timing13 writes the multiplier
field first and then dynamically enables that control only when the effective
multiplier differs from KVM's default ratio. A later userspace or nested-guest
ratio change therefore turns scaling back on before VM-entry.

With identity scaling disabled, architectural guest TSC remains
`host_tsc + TSC_OFFSET`; CPUID values, TSC offset, PMU switching, and all VMCS
safety checks are unchanged. This does not compensate or falsify elapsed time.
The patch applies and reverses cleanly against unmodified 7.1.3-1 sources. The
full focused Clang/LLVM build, installed-kernel ABI modpost, final `.ko` link,
BTF generation, compression, checksum generation, and staging all complete
successfully; the build-only validation did not change system modules. Its
SHA256 is
`2a1cb70212646fb1297ea166d128ea377a5573a8d9454492b6099464b1c8f1b1`.

Install from fish:

```fish
./kvm.sh install --yes \
  --patch kvm/patches/7.1.3-1.intel.v2-timing13-default-tsc-scaling.patch \
  --work-dir .work/v2-timing13-default-tsc-scaling --jobs 8
```

After reboot and VM startup, use timing12's same three host switches and run
the already-built guest tool once:

```bat
cpuid_latency.exe --transition-burst 2 200000
```

Then collect `timing13-default-tsc-scaling.txt` with
`tools/cpuid_inner_diag.sh` and disable the profiler. The vCPU2 VMCS snapshot
must show secondary controls without bit 25 (the previous `0x061017eb` should
become `0x041017eb`) while the TSC ratio remains `281474976710656/48`.

The first installed timing13 capture confirmed this on all six vCPUs. For the
benchmark vCPU2, 196 samples measured VMRESUME-to-next-CPUID-VMEXIT at
`612/619.78/928` minimum/average/maximum host TSC cycles, versus timing12's
203-sample `612/645.00/2852`. The average fell by 25.22 cycles (3.91%), while
the unchanged minimum means the change cannot be attributed to TSC-scaling
elision. The first-sample entry/exit snapshots also occurred in a different
guest execution mode (dynamic EFER/IA32e controls differed), so their non-TSC
control differences are not a patch effect. Later measurements did not
establish a repeatable guest-visible benefit. Timing13 is therefore a rejected
experiment retained only for reproduction; timing16 removes the control
elision and returns to KVM's normal TSC-scaling behavior.

## Intel v2 timing14 VM-entry/exit state-load audit (2026-09-13)

`7.1.3-1.intel.v2-timing14-state-load-audit.patch` is a cumulative timing13
diagnostic patch. It does not change any VMCS control or guest-visible state.
When the existing sampled CPUID profiler observes direct hits during one KVM
run, timing14 reads the active dedicated state fields only after returning from
guest execution, outside the measured inner transition. This ties the snapshot
to the actual sampled CPUID window instead of an unrelated first profile-enabled
VM-entry.

The audit covers `IA32_PERF_GLOBAL_CTRL`, `IA32_PAT`, `IA32_EFER`, and the CET
state group (`S_CET`, `SSP`, and interrupt SSP table). Per-vCPU debugfs counters
record how often each state group was active and how often its guest and host
VMCS values were identical. The latest active/equal masks use `0x1` for PERF,
`0x2` for PAT, `0x4` for EFER, and `0x8` for CET. Exact latest guest/host values
are also exported. These observations distinguish a potentially redundant
state load from a load that is architecturally required; they do not by
themselves disable anything.

The cumulative patch applies and reverses cleanly against unmodified 7.1.3-1
sources. The full focused Clang/LLVM build, installed-kernel ABI modpost, final
`.ko` link, BTF generation, compression, checksums, and staging all completed
successfully without changing the installed modules. Its SHA256 is
`df51e79662a513549e659495a0b754c6487e4d0d0abeba65927d8a1c975b2f72`.

Install from fish:

```fish
./kvm.sh install --yes \
  --patch kvm/patches/7.1.3-1.intel.v2-timing14-state-load-audit.patch \
  --work-dir .work/v2-timing14-state-load-audit --jobs 8
sudo reboot
```

After reboot and VM startup, arm the unchanged timing13 fast path and profiler:

```fish
printf 'Y\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_early
printf 'Y\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_skip_rfds_pcore
printf 'Y\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_profile
```

Run one larger burst in the Windows guest; no rebuild of the guest tool is
required:

```bat
cpuid_latency.exe --transition-burst 2 1000000
```

Collect once and disable profiling from fish:

```fish
sudo bash kvm/tools/cpuid_inner_diag.sh | tee ~/桌面/ttest/timing14-state-load-audit.txt
printf 'N\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_profile
```

The decisive section is `sampled VM-entry/exit state-load audit`. A state whose
`active_samples` and `equal_samples` remain equal throughout the benchmark is a
candidate for a later semantics-preserving lazy switch; a differing state must
continue to use the hardware transition control.

The first installed capture produced 1182 profiled direct hits and 316
state-window observations on vCPU2. PERF was active and identical in all
`316/316` observations (`guest=host=0`). PAT was active but unequal in
`316/316`, and CET was active but unequal in `316/316`; both must remain
switched. EFER was dynamically active in only 2 observations and unequal in
both, confirming that existing KVM EFER elision is already working. All other
vCPUs independently showed the same decisive pattern: every active PERF sample
was equal, while no active PAT, EFER, or CET sample was equal. This isolates the
three dedicated PERF_GLOBAL_CTRL operations (guest load, guest save, and host
load) as the next candidate. A correct optimization must trap the first guest
write while the controls are elided and restore hardware switching before the
next entry; simply clearing the controls would leak guest PMU state into the
host.

## Intel v2 timing15 guarded lazy PERF switching (2026-09-13)

`7.1.3-1.intel.v2-timing15-lazy-perf.patch` is a cumulative timing14 patch. It
adds the opt-in `fast_cpuid_inner_lazy_perf` experiment based on timing14's
316/316 equal PERF observations. It affects only a non-nested, non-SMM run
window already eligible for the CPUID inner fast path, and only when the vCPU
has a mediated PMU, hardware supports all three dedicated PERF controls, the
software and both VMCS PERF_GLOBAL_CTRL values are zero, and the original entry
load plus exit save/load controls are present.

KVM's mediated-PMU load path explicitly writes hardware PERF_GLOBAL_CTRL to
zero before entering vendor code. Timing15 first forces interception of guest
writes to PERF_GLOBAL_CTRL, then clears the VM-entry guest load and VM-exit
guest-save/host-load controls. A guest attempt to enable the PMU therefore exits
before executing the WRMSR. On every return from the run window, timing15
restores all three controls before restoring KVM's normal MSR-intercept policy
and before the non-CPUID exit is handled. Nested VMCS construction, SMM, PMU
activity, non-zero state, missing controls, and every ordinary fallback retain
the original KVM path. PAT, EFER, CET, CPUID values, and timing compensation are
unchanged.

The feature defaults off. The cumulative patch applies and reverses cleanly
against unmodified 7.1.3-1 sources. The full focused Clang/LLVM build,
installed-kernel ABI modpost, final `.ko` link, BTF generation, compression,
checksums, and staging completed successfully without changing installed
modules. Its SHA256 is
`ed6eff8da7a7023aadfb06522dff3e95115b3b1a299090b212bbdedd5f1d801e`.

Install from fish:

```fish
./kvm.sh install --yes \
  --patch kvm/patches/7.1.3-1.intel.v2-timing15-lazy-perf.patch \
  --work-dir .work/v2-timing15-lazy-perf --jobs 8
sudo reboot
```

After reboot and VM startup, enable the three established switches, timing15,
and the profiler:

```fish
printf 'Y\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_early
printf 'Y\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_skip_rfds_pcore
printf 'Y\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_lazy_perf
printf 'Y\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_profile
```

Run one guest burst without rebuilding the Windows tool:

```bat
cpuid_latency.exe --transition-burst 2 1000000
```

Collect and return the two experimental switches to off from fish:

```fish
sudo bash kvm/tools/cpuid_inner_diag.sh | tee ~/桌面/ttest/timing15-lazy-perf.txt
printf 'N\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_profile
printf 'N\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_lazy_perf
```

The sampled vCPU2 snapshot should change from exit/entry controls
`0x500bffff/0x1073ff` to `0x100befff/0x1053ff`, with secondary controls still
`0x041017eb`. Its state audit should show PERF inactive while PAT and CET remain
active (`latest_active=0xa`). The `cpuid_inner_lazy_perf_windows` and
`cpuid_inner_lazy_perf_restores` counters must both be non-zero and equal. The
decisive performance comparison is vCPU2's timing14 transition baseline
`610/629.27/2912`.

The 2026-09-13 guest run in
`~/桌面/ttest/timing15-lazy-perf.txt` validated the guard and restore path:
vCPU2 recorded `406978` windows and `406978` restores, the sampled controls
were `exit=0x100befff entry=0x1053ff`, and PERF was inactive in all 281 state
samples (`latest_active=0xa`).  However, the measured vCPU2 transition was
`606/679.26/3520` over 1003 samples.  Relative to timing14, the average
increased by 7.95% while the minimum improved by only four cycles.  Dedicated
PERF_GLOBAL_CTRL switching is therefore not a demonstrated latency bottleneck
on this machine. Timing15 is a rejected experiment retained only for
reproduction. Its `fast_cpuid_inner_lazy_perf` switch and all associated
begin/end code and counters are absent from timing16 and later optimization
patches. An already-installed timing15 module must keep the switch at `N`.

## Intel v2 timing16 deferred early-output synchronization (2026-09-13)

`7.1.3-1.intel.v2-timing16-deferred-reg-sync.patch` starts a clean cumulative
line from timing12's established CPUID path and timing14's read-only state-load
audit. It deliberately excludes both rejected transition-control experiments:
identity TSC-scaling elision from timing13 and lazy PERF_GLOBAL_CTRL switching
from timing15. Consequently it has no `fast_cpuid_inner_lazy_perf` parameter,
and the normal KVM TSC-scaling control policy is restored.

The new optimization targets confirmed work in the approximately 30-cycle
`pre_resume` bucket without removing a VMCS safety check. On every successful
pre-spill CPUID hit, the old path copied EAX, EBX, ECX, and EDX into both the
live hardware GPRs and `vcpu->arch.regs`. The four canonical-array stores were
needed only if the immediately following `VMRESUME` failed; after a successful
entry, a later ordinary exit already saves all live GPRs before returning to C.
Timing16 therefore keeps the four CPUID outputs live in hardware and marks that
state in an assembly-only run flag. Only the VM-Fail path copies those four
registers into the canonical array, before RBX is replaced by KVM's failure
return value. Cache generation, CPUID values, RIP advancement, debug/event
checks, mitigations, and fallback behavior are unchanged. No new runtime
optimization switch is added.

The cumulative patch applies and reverses cleanly against the unmodified
7.1.3-1 source. Structural tests pass, Clang/LLVM builds the changed C and
assembly objects, installed-kernel ABI modpost succeeds, and the final modules
complete link, BTF generation, compression, checksum generation, and staging.
The built module exports the established CPUID switches, no lazy-PERF switch,
and contains no TSC control-elision code. The patch SHA256 is
`1c2d3ccf568151b822e0175e6c18e8ba4bfd2f18305369d7b6bd12b99b311810`.

Install from fish after closing the VM:

```fish
./kvm.sh install --yes \
  --patch kvm/patches/7.1.3-1.intel.v2-timing16-deferred-reg-sync.patch \
  --work-dir .work/v2-timing16-deferred-reg-sync --jobs 8
sudo reboot
```

After reboot and VM startup, enable the established early/RFDS paths and the
temporary profiler:

```fish
printf 'Y\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_early
printf 'Y\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_skip_rfds_pcore
printf 'Y\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_profile
```

Run the existing guest binary once:

```bat
cpuid_latency.exe --transition-burst 2 1000000
```

Collect from fish and disable profiling:

```fish
sudo bash kvm/tools/cpuid_inner_diag.sh | tee ~/桌面/ttest/timing16-deferred-reg-sync.txt
printf 'N\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_profile
```

The primary optimization metric is vCPU2's `pre_resume` average versus the
roughly 30-cycle timing12--timing15 range. The transition snapshot should again
contain TSC scaling and normal PERF controls; that confirms removal of the two
rejected experiments rather than a regression in the new GPR path. Guest CSV
and the external timing detector remain the final end-to-end checks.

The first installed capture produced 1963 vCPU2 samples. The intended
`pre_resume` metric increased from timing14's 29.98 cycles to 32.42 cycles
(+8.14%), so deferring the four stores did not shorten their dependency path.
The raw transition average was 700.61 cycles because one 136180-cycle interrupt
outlier dominated the sum; excluding only that maximum gives 631.52 cycles,
versus timing14's similarly adjusted 627.34 cycles. The minimum remained in the
same floor band at 608 cycles. The snapshot correctly restored normal PERF and
TSC controls (`exit=0x500bffff`, `entry=0x1073ff`,
`secondary=0x061017eb`) and the rejected lazy-PERF parameter was absent. Thus
the cleanup is correct, but deferred register synchronization has no measured
positive effect and must not be carried into the next optimization patch.

## Intel v2 timing17 per-vCPU CPUID MRU lookup (2026-09-13)

`7.1.3-1.intel.v2-timing17-mru-cache.patch` continues from the cleaned
timing12 path plus timing14's read-only diagnostics. It does not include
timing13's identity TSC-scaling control change, timing15's lazy PERF controls,
or timing16's deferred GPR synchronization. No VMCS validation is removed and
no new module option is added.

The optimization targets the measured lookup bucket rather than the hardware
VM-entry/exit transition. Each vCPU retains a pointer to its most recently
matched canonical CPUID cache entry. A following CPUID first validates that
entry's valid bit and function, and for indexed leaves also its subleaf. An
exact match therefore bypasses both multiplicative hash calculations and the
direct-map address calculation. A miss continues through the unchanged
non-indexed and indexed hash paths. Both early and post-spill cache hits update
the pointer.

The pointer is cleared before each static or dynamic cache rebuild, before the
embedded entry array is overwritten. It never points outside that per-vCPU
array, and returned values still come from the existing KVM-derived cache.
Non-indexed leaves continue to ignore ECX; indexed leaves still require an
exact ECX match. This preserves generation invalidation and CPUID semantics.
The tradeoff is a few extra comparisons on MRU misses, so the result must be
judged separately for repeated-leaf bursts and mixed-leaf workloads.

The cumulative patch applies and reverses cleanly against unmodified
7.1.3-1. Structural checks pass; Clang/LLVM compiles the changed C and assembly
objects; installed-kernel ABI modpost, final module linking, BTF generation,
compression, checksums, and staging all completed. The built module has normal
TSC/PERF behavior and exports none of the rejected experiment switches. Patch
SHA256:
`a1f6f50cccd3e77733308f1d9779554cdd2e76e83b15779087ddb3160fef763e`.

Install from fish after shutting down the VM:

```fish
./kvm.sh install --yes \
  --patch kvm/patches/7.1.3-1.intel.v2-timing17-mru-cache.patch \
  --work-dir .work/v2-timing17-mru-cache --jobs 8
sudo reboot
```

After reboot and VM startup, enable the established paths and profiler:

```fish
printf 'Y\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_early
printf 'Y\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_skip_rfds_pcore
printf 'Y\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_profile
```

Run the existing Windows guest binary once:

```bat
cpuid_latency.exe --transition-burst 2 1000000
```

Save the guest CSV as `timing17-mru-cache.csv`, then collect the host snapshot
and turn profiling back off from fish:

```fish
sudo bash kvm/tools/cpuid_inner_diag.sh | tee ~/桌面/ttest/timing17-mru-cache.txt
printf 'N\n' | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_profile
```

The primary internal comparison is vCPU2's lookup average against timing16's
37.96 cycles (and timing14's 37.70-cycle clean reference). The transition and
`pre_resume` buckets should remain in their previous bands; the guest CSV and
the external detector are the decisive end-to-end measurements. A lower
lookup value only in the repeated-leaf burst would confirm the intended MRU
effect without establishing a general mixed-leaf improvement.

The first timing17 capture (`~/桌面/ttest/timing17-mru-cache.txt`) reached
1011 vCPU2 samples. It measured `validation=80.67`, `lookup=35.45`,
`rip/vmwrite=35.45`, and `pre_resume=30.30` cycles. Lookup was 6.61% below
timing16's 37.96-cycle result, while the summed inner buckets changed by less
than 1%; this is a modest lookup-path improvement, not a complete transition
improvement. The transition minimum was 612 cycles and the raw average was
897.34 cycles, dominated by a 221892-cycle outlier. Excluding only that maximum
still gives 678.32 cycles, above timing14's 629.27-cycle capture, so timing17
is not yet an end-to-end win.

This run also coincided with repeated Btrfs checksum/direct-I/O errors on
`nvme1n1p3`. Root 257 resolves inode `11244065` to
`/home/lx/桌面/win10.img` (a 240-GiB raw Windows image, about 131 GiB
allocated); the counter was already 16308 at mount and reached 16315. The
NVMe SMART log currently reports 0 media/data-integrity errors and 0 logged
controller errors, so this is confirmed file-level corruption but not proof of
an SSD NAND failure. Back up the image and investigate power-loss/RAM/firmware
or prior-write causes before further timing runs. Treat the transition averages
and the empty UTF-16 burst-marker CSV as inconclusive until the storage issue
is diagnosed and the same benchmark is repeated on a clean run. No VMware
verdict was recorded in that CSV.

## Intel v2 timing6 sampled CPUID profiler (2026-09-11)

`7.1.3-1.intel.v2-timing6-profile.patch` keeps all timing5 behavior and adds
an opt-in, one-in-1024 sampled cycle profiler. The default is off, so the
timing5 path is unchanged until profiling is explicitly enabled. Its SHA256 is
`d3104d54e06af93be6f9b1e2fee286d8ec820e8f0a08d0dd526adc72a54b8c25`.

Build/install it as the next cumulative baseline:

```bash
./kvm.sh install --yes \
  --patch kvm/patches/7.1.3-1.intel.v2-timing6-profile.patch \
  --work-dir .work/v2-timing6-profile --jobs 8
```

After reboot, explicitly restore the timing5 switches, then arm only the
profiler:

```bash
P=/sys/module/kvm_intel/parameters
echo Y | sudo tee $P/fast_cpuid
echo Y | sudo tee $P/fast_cpuid_direct
echo Y | sudo tee $P/fast_cpuid_inner
echo Y | sudo tee $P/fast_cpuid_inner_skip_rfds_pcore
echo Y | sudo tee $P/fast_cpuid_inner_early
echo Y | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_profile
# Keep the experimental relaxed switch disabled for normal VM operation.
echo N | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_relaxed
```

Run the CPUID benchmark once, then collect one diagnostic snapshot:

```bash
sudo bash kvm/tools/cpuid_inner_diag.sh > ~/桌面/ttest/timing6-profile-diag.txt
```

The profiler stores stage endpoints for each sample and commits a sample only
after a complete direct hit, so fallback exits cannot produce negative or
cross-sample stage deltas. The diagnostic script prints per-vCPU averages and
marks the largest measured software segment automatically. Profiling is
diagnostic only; disable it before final latency measurements with `echo N`.

Run `python3 test_kvm_cpuid_timing6_profile.py` for structural checks.

## Intel v2 timing4 RFDS/P-core experiment (2026-09-10)

`7.1.3-1.intel.v2-timing4.patch` is a complete patch against the official
7.1.3-1 source. Its validated SHA256 is
`f1878e0232f0204ada480b474f646cea5b1a49997e810702d75345a0b43c3ac0`.

Timing3 still executes the kernel's conditional `VERW` sequence before every
direct VMRESUME. On hybrid Intel packages RFDS is enumerated package-wide even
though the affected CPU type is the Atom/E-core side. Timing4 adds one narrowly
guarded experiment which can omit that RFDS-only `VERW` when the vCPU is
currently executing on an Intel performance core. It refuses the omission if
MDS, TAA, or MMIO stale-data mitigation can also require buffer clearing. The
switch defaults to `N`, so the default behavior and security semantics are
identical to timing3.

Build/install explicitly, close running VMs first, and reboot afterwards:

```bash
./kvm.sh install --yes \
  --patch kvm/patches/7.1.3-1.intel.v2-timing4.patch \
  --work-dir .work/v2-timing4 --jobs 8
```

Keep vCPUs pinned to P-cores. Then measure the same workload twice, changing
only this runtime switch:

```bash
# timing3-equivalent control
echo N | sudo tee \
  /sys/module/kvm_intel/parameters/fast_cpuid_inner_skip_rfds_pcore

# timing4 experiment
echo Y | sudo tee \
  /sys/module/kvm_intel/parameters/fast_cpuid_inner_skip_rfds_pcore
```

Use a fresh benchmark process and save a separate CSV for each state. Static
cache-hit leaves should move together; dynamic/fallback leaves should remain a
control group. `cpuid_inner_rfds_skip_armed=1` in the diagnostic output proves
that the kernel armed the assembly skip on the vCPU's current core. If there is
no repeatable improvement, restore `N`. Run
`python3 test_kvm_cpuid_timing4.py` to check the opt-in default, vulnerability
guards, per-core type guard, run flag, and assembly ordering.

## Intel v2 generic CPUID timing3 experiment (2026-09-10)

`7.1.3-1.intel.v2-timing3.patch` is a complete patch against the official
7.1.3-1 source, not an incremental patch. Its validated SHA256 is
`c474b2b7a1fac651487ec93751190ff4444784bff03ac8c25da9c83226c12225`.

This revision is not tailored to a VMAware threshold or to CPUID leaf 0. It
builds a bounded 256-slot per-vCPU cache for every CPUID model entry whose
result is provably static and preserves both indexed and non-indexed
first-match behavior. Stateful functions, mixed lookup modes, runtime-adjusted
functions 1, 7, 0xD and 0x80000007, unknown leaves, and cache collisions beyond
eight probes retain the timing2 C path.

The cache hashes the function and index with independent multiplicative mixes.
Using their low bytes directly caused dense basic, hypervisor, extended and
indexed CPUID ranges to form long probe clusters, silently dropping otherwise
eligible entries. The 2026-09-10 CSV comparison exposed this because leaves 0,
0xA, 0xB and 0x80000000 hit while static leaves 0x1F and 0x80000001 did not.

On a cache hit, the x86-64 VMX assembly validates the complete exit reason,
event-vectoring state, STI/MOV-SS shadow and guest TF, advances RIP using the
hardware instruction length, writes zero-extended CPUID results, accounts the
exit, and directly executes VMRESUME. This removes the general C exit/re-entry
round trip but not Intel's mandatory CPUID VM exit. It does not rewrite guest
code, compensate clocks, or alter TSC behavior.

The inner path is disabled for nested guests, SMM, CPUID faulting, userspace or
guest-owned debug state, dirty dynamic CPUID state, Xen CPUID, instruction-
retired PMU counting, active KVM entry/exit/CPUID tracing, and L1D flushing.
CPU-buffer clearing is preserved by replaying the standard conditional VERW
sequence immediately before every direct VMRESUME. These fallbacks preserve
correctness and security semantics.

Install explicitly, close running VMs first, and reboot afterwards:

```bash
./kvm.sh install --yes \
  --patch kvm/patches/7.1.3-1.intel.v2-timing3.patch \
  --work-dir .work/v2-timing3 --jobs 8
```

Three switches provide four comparable modes. Use a fresh benchmark process
for each measurement and restore all switches to `Y` afterwards:

```bash
# timing3: inner VMX cache path
echo Y | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid
echo Y | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_direct
echo Y | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner

# timing2 equivalent
echo N | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner

# timing1 equivalent
echo N | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_direct

# original slow path
echo N | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid
```

Do not evaluate this revision from one detector result alone. Build
`kvm/tools/cpuid_latency.cpp` in the guest and compare all 15 leaf/subleaf rows
in each mode; eligible static leaves and intentional dynamic/fallback leaves
should form different groups. `python3 test_kvm_cpuid_timing3.py` checks generic
cache lookup, collision/wrap behavior, unsafe-function fallback, and assembly
guards. The timing1/timing2 GCC and Clang decision tests also pass.

The inner path has low-overhead per-vCPU observability. Read the aggregate
debugfs counters while the VM is running:

```bash
sudo find /sys/kernel/debug/kvm -type f \
  -path '*/vcpu*/cpuid_inner_*' \
  -exec grep -H . {} +
```

- `cpuid_inner_fastpath` counts real assembly cache hits.
- `cpuid_inner_cache_miss` counts CPUID exits that reached the assembly lookup
  but had no safe cached entry.
- `cpuid_inner_blockers` is the latest eligibility bitmask. Zero means enabled;
  bits `0x1/0x2/0x4/0x8/0x10/0x20/0x40/0x80/0x100/0x200` mean respectively
  disabled switch, immediate exit, tracing, L1D flush, nested/SMM, debug state,
  CPUID faulting, dirty dynamic state, retired-instruction PMU, and Xen.

For short diagnostic runs only, enable rate-limited blocker transition logs
before starting the VM, then disable them after collecting the output:

```bash
echo Y | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_debug
sudo journalctl -k -f | grep --line-buffered 'CPUID inner'
echo N | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_inner_debug
```

Never add unconditional printk calls to the CPUID path: logging overhead would
invalidate the timing measurement.

`kvm/tools/cpuid_inner_diag.sh` collects the loaded module identity, switches,
relevant mitigations, decoded per-vCPU counters and recent transition logs:

```bash
sudo bash kvm/tools/cpuid_inner_diag.sh
sudo bash kvm/tools/cpuid_inner_diag.sh --enable-log
# Start the VM and reproduce once, then collect again and disable logging.
sudo bash kvm/tools/cpuid_inner_diag.sh
sudo bash kvm/tools/cpuid_inner_diag.sh --disable-log
```

Validation completed: strict forward/reverse patch checks, checkpatch,
Clang/LLVM compilation, objtool, modpost, final linking, module checksum and
7.1.3-1-cachyos vermagic checks all pass. The six focused upstream KVM
selftests and the 17-case guest-owned TF/DR0-DR3 regression test compile, but
runtime validation is still pending because `/dev/kvm` is unavailable in the
build sandbox. No system modules were installed.

## Intel v2 CPUID timing experiment (2026-09-09)

`7.1.3-1.intel.v2-timing2.patch` is a separate second-stage experiment. It
includes the complete corrected timing patch and adds two independently
switchable optimizations:

Its validated SHA256 is
`0c05bc46bd609a7804e00847357ddbb6d622f70c128a56f0a77e12ddd592de84`.

- `fast_cpuid=Y`: the existing guarded standard re-entry fastpath.
- `fast_cpuid_direct=Y`: VMX-local completion avoids repeated x86 vendor
  dispatch, while retaining KVM's instruction-length/RIP, interrupt-shadow,
  PMU retirement, tracing, and CPUID model behavior.
- CPUID leaf 0 uses a per-vCPU immutable cache populated after
  `KVM_SET_CPUID2`; every other leaf continues through the live KVM model and
  its dynamic fixups.

Install it explicitly and reboot:

```bash
./kvm.sh install --yes \
  --patch kvm/patches/7.1.3-1.intel.v2-timing2.patch \
  --work-dir .work/v2-timing2 --jobs 8
```

Use fresh detector processes for this three-step A/B comparison:

```bash
# timing2: direct completion plus leaf-0 cache
echo Y | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_direct

# timing1-equivalent standard fastpath
echo N | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid_direct

# original slow path
echo N | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid
```

Restore both switches to `Y` afterwards. The cache is used only by direct
mode. `test_kvm_cpuid_timing2.py` compiles the actual helpers with GCC and
Clang and checks 22 guarded dispatch cases in both modes, cached leaf 0,
instruction retirement ordering, and CPUID table first-match semantics.
These are decision tests, not VMX hardware tests.

The detector threshold in the bundled VMAware 2.8.1 source is 2.5. A measured
2634/231 ratio of 11.40 would need a CPUID window below roughly 578 at the same
reference rate. Intel VMX makes CPUID exit unconditionally, so C-level
optimization cannot be assumed to reach that target. This experiment measures
the remaining software component before considering a substantially riskier
inner VMX re-entry loop.

The timing variant is a complete patch against official 7.1.3-1 sources, not
an incremental patch to stack on v2. The tested v2 and the default patch remain
unchanged. Select the experiment explicitly:

```bash
./kvm.sh install --yes \
  --patch kvm/patches/7.1.3-1.intel.v2-timing.patch \
  --work-dir .work/v2-timing --jobs 8
```

Close VMs before installing and reboot afterwards. Its SHA256 is
`4136aff685d23ec5915586dd39febc18694e9c9a426a375d49f426c3986a688e`.

The extra handler uses `kvm_emulate_cpuid()` and the existing
`EXIT_FASTPATH_REENTER_GUEST` loop. Eligible exits retain the mediated PMU
context instead of doing `kvm_mediated_pmu_put()` / `load()` for every CPUID.
All leaves still use KVM's CPUID model, including undefined-leaf handling.
There is no host-CPUID cache, guest instruction rewriting, TSC compensation,
cross-vCPU pausing, affinity change, or removal of speculation mitigations.
The standard re-entry loop still processes pending requests and interrupts.

Nested guests, SMM, userspace debugging, guest RFLAGS.TF, unsynchronized guest
debug registers, CPUID faulting, dirty dynamic CPUID state, exceptional exit
flags, and Xen CPUID configurations retain the slow
path. So do mediated PMUs actively counting retired instructions, following
KVM's existing PMU fastpath guard. This preserves accounting rather than
making CPUID artificially disappear from PMU counters. These conditions can
reduce or eliminate the performance gain; the fallback is intentional.

The initial timing revision omitted guest-owned single stepping and lazy
debug-register synchronization. VMAware's "hypervisor interception" check
uses TF plus a guest DR0 execution breakpoint, not KVM_SET_GUEST_DEBUG.
Running CPUID emulation before the slow path synchronizes DR0-DR3/DR7 can
make the existing #DB emulation miss a DR6 breakpoint bit. This revision
routes both TF and KVM_DEBUGREG_WONT_EXIT through the original path; it does
not change the stable v2 debug behavior. Hardware confirmation of this fix
is still pending.

After reboot, `fast_cpuid` should read `Y`. It is runtime-switchable for A/B
tests without reinstalling or restarting the guest:

```bash
cat /sys/module/kvm_intel/parameters/fast_cpuid
echo N | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid
# Run the guest benchmark in a NEW process and save the baseline.
echo Y | sudo tee /sys/module/kvm_intel/parameters/fast_cpuid
# Run the same benchmark again in a NEW process, with unchanged VM settings.
```

The switch affects all Intel VMs on this host and resets to `Y` on module reload
unless configured otherwise. KVM statistics expose `cpuid_fastpath` and
`cpuid_fastpath_fallback`; the latter counts guarded fallbacks while enabled,
not disabled-mode or nested exits. If debugfs is already mounted:

```bash
sudo find /sys/kernel/debug/kvm -type f -name 'cpuid_fastpath*' -exec grep -H . {} +
```

Validation: strict forward/reverse application, Clang/LLVM compilation,
modpost, final linking and both modules' 7.1.3-1-cachyos vermagic passed.
`python3 test_kvm_cpuid_fastpath.py` tests 22 decision scenarios by compiling
the actual handler against mocks (GCC and Clang checked). This is not a
hardware correctness test. Upstream `cpuid_test`, `cr4_cpuid_sync_test`,
`xcr0_cpuid_test`, `debug_regs`, `pmu_counters_test`, and `vmx_pmu_caps_test` were built under
`.work/v2-timing/selftests/x86/`; execution is pending because `/dev/kvm` is
unavailable in the build sandbox. No system modules were installed.

`tools/guest_debug_cpuid.c` adds 17 guest-owned debug regression cases: TF
alone, DR0-DR3 local/global execution breakpoints, and their combinations
with TF. It deliberately never uses KVM_SET_GUEST_DEBUG. The combined DR6
BS/Bn expectation tests this Intel patch's behavior, not generic upstream
KVM. Build and run on the host with the patched modules loaded:

```bash
src="$PWD/.work/v2-timing/builds/7.1.3-1-cachyos-4136aff685d2/cachyos-7.1.3-1"
out="$PWD/.work/v2-timing/selftests"
cp kvm/tools/guest_debug_cpuid.c "$src/tools/testing/selftests/kvm/x86/ovo_guest_debug_cpuid.c"
make -s -C "$src/tools/testing/selftests/kvm" OUTPUT="$out" \
  TEST_GEN_PROGS_x86=x86/ovo_guest_debug_cpuid -j8 \
  "$out/x86/ovo_guest_debug_cpuid"
sudo "$out/x86/ovo_guest_debug_cpuid"
```

This test compiled successfully, and disassembly confirms POPFQ directly
precedes CPUID and the breakpoint target immediately follows it. Execution
here returned SKIP (exit 4), not PASS, because `/dev/kvm` is unavailable.

`tools/cpuid_latency.cpp` is an independent Linux/Windows x64 probe for 15
leaf/subleaf inputs, using both a cross-core software counter and separate
TSC windows. Build instructions are in its header; run it with measurement
CPU, counter CPU, and optional sample count, e.g. `cpuid_latency 2 3 2000`.
Use two distinct physical cores of the same type. It reports distributions,
not VMAware verdicts. Linux GCC/Clang builds and native execution were checked;
the Windows build and patched-guest measurements remain to be tested.

This is a real-overhead reduction experiment, not a claim of invisible VM
timing. Intel VMX still forces CPUID exits (Intel SDM Vol. 3, section 26.1.2).
VMAware 2.8.1's local `timer()` uses another core's memory counter, not TSC,
and compares CPUID.0 against SERIALIZE/LFENCE. Changing TSC cannot change that
counter's progress. See the [Intel SDM](https://cdrdv2-public.intel.com/671506/326019-sdm-vol-3c.pdf)
and [USENIX's analysis of concurrent timing sources](https://www.usenix.org/legacy/event/hotos07/tech/full_papers/garfinkel/garfinkel_html/paper.html).
The user's initial guest comparison reported 6038/234 (ratio 25.8034) before
and 2634/231 (ratio 11.4026) with the first timing revision: about 56% lower
CPUID measurement, but timing was still detected and the debug regression
above appeared. This is one comparison, not a controlled performance claim.
The corrected revision still needs guest measurements; other timing
detectors can still observe VM exits.

## CachyOS 7.1.3 Intel port (2026-09-08)

`7.1.3-1.intel.patch` ports the complete 6.19.14 Intel revision to the
official `linux-cachyos 7.1.3-1` source:

```text
SHA256: 37d87a62b4c99f3d1ec3551f1425077d50309da3c48ab56b833662a886806f56
```

The port keeps the 7.1.3 non-const `struct kvm_pmu_ops *` interface and its
mediated-PMU initialization. The synthetic P-core capability explicitly leaves
the mediated backend disabled. It also attaches the dummy DS-area lifetime to
the 7.1.3 `x86_virt_get_ref()` / `x86_virt_put_ref()` wrappers, including
allocation and VMX-enable failure cleanup, instead of modifying the shared
`arch/x86/virt/hw.c` VMX implementation.

Validation used the installed `7.1.3-1-cachyos` configuration and ABI. The
patch applied cleanly to the cached official source, reverse verification
passed, focused `kvm.o` and `kvm-intel.o` compilation succeeded, and custom
modpost/final linking produced both modules with matching
`7.1.3-1-cachyos` vermagic. No system modules were installed. The inherited
unused VMX helper and assembly fast-path objtool warnings remain.

A subsequent semantic review found that Linux 7.0's dynamic intercept
recalculation would re-enable `CPU_BASED_RDPMC_EXITING` after vCPU CPUID is set.
The port now clears the bit in both the initial VMCS controls and
`vmx_recalc_instruction_intercepts()`, preserving the unconditional direct-RDPMC
behavior of the 6.19 patch. The rebuilt object and module show the expected
bit-clear operation; runtime VM-exit observation remains recommended after
installation.

The upstream KVM changes between 6.19 and 7.1.3, including their impact on
this port, are documented in [KVM-CHANGES-6.19-TO-7.1.3.md](KVM-CHANGES-6.19-TO-7.1.3.md).

## Official 6.19.14 release (2026-09-07)

The sole 6.19.14 patch is based on the user-tested complete Nika revision,
formerly named `6.19.14-nika-fixed.intel.patch`. After promotion,
`allow_hybrid_pmu` now defaults to true inside the module, and hybrid PMU
capability validation selects online Intel P-core CPUs automatically:

```text
SHA256: e2f97131393aa86b67cb3dbc465c1f8c9770dd879a0b6f81f9acbbac4e87e827
```

It retains original Nika except for the orphan LBR/timer guard fix, extended
CPUID cache-stride fix, hybrid vPMU capability checks, and HFI/ITD/HRESET
enumeration filtering. Both `enable_pmu` and `allow_hybrid_pmu` now default to
true; a separate modprobe configuration is not needed to enable them.
The tested P-core pinning requirement remains. This is not support for arbitrary
P/E-core migration or a per-VM choice of PMU type.
There is no `arch_lbr` or `nika_features` option.

On hybrid CPUs the helper reads CPUID.1A on each online logical CPU, selects
`INTEL_CPU_TYPE_CORE` (0x40), and reads/compares CPUID.0A only on those P-core
CPUs. CPU numbers, core counts and SMT layout are not hard-coded. E-core PMU
differences no longer disable the P-core vPMU backend. All selected online
P-core CPUs must still agree; missing P-cores, invalid capabilities or a failed
cross-CPU read leave the backend disabled. Unknown core types are not selected.

The module logs `hybrid vPMU validated P-core CPUs: ...`. This is a load-time
snapshot for the global KVM PMU capability, not an inspection of a particular
VM's affinity. Bind vCPU threads to a subset of that validated set in the VM
configuration. The patch neither changes nor enforces affinity, and does not
revalidate CPUs brought online later; do not expand the VM's affinity to such
CPUs without reloading/rebooting and validating again. The previous `0-5`
setting was specific to the tested host's VM configuration, not a patch limit.

Explicit `enable_pmu=0` or `allow_hybrid_pmu=0` still overrides the default.
An incompatible PMU still leaves the KVM vPMU backend disabled. Non-hybrid
CPUs continue through the ordinary capability path. The inherited raw CPUID
and physical-MSR paths are otherwise unchanged; this is not a general PMU
fallback or ownership fix.

Existing `options kvm enable_pmu=1 allow_hybrid_pmu=1` settings are redundant
but harmless for this module. They can be removed from modprobe configuration;
do not remove unrelated options from the same file. Also check the kernel
command line for explicit disable parameters. The installer does not create,
edit or manage any such configuration files.

All other 6.19.14 patch variants and the obsolete integrity generator/tests
have been removed. Other kernel versions remain. The external Nika original,
historical build artifacts and `/var/backups/ovo-kvm` were not deleted.
The filename promotion alone required no reinstall. The subsequent default and
P-core selection changes require building/installing the new modules and rebooting to load
them; it does not change a module that is already loaded. After reboot:

```bash
cat /sys/module/kvm/parameters/enable_pmu
cat /sys/module/kvm/parameters/allow_hybrid_pmu
journalctl -k -b --grep='hybrid PMU|hybrid vPMU|unknown parameter'
```

Both values should be `Y` when no disable override exists and capability
validation succeeds. Previous Windows stability feedback applies to the
pre-default-change revision; no additional VM run is implied by this update.

Validation of this revision: the patch applied to fresh Linux 6.19.14 sources
and both KVM modules built successfully. An extracted-C helper test covered
sparse/interleaved P-core numbering, different E-core PMUs, mismatched P-core
registers, absent/unknown/offline CPUs, failure-path cleanup, explicit disable
options and the unchanged non-hybrid path. Non-PMU patch hunks are byte-identical
to the preceding default-on revision. No system modules were installed during
these checks; inherited VMX/objtool build warnings remain.

## MONITOR/MWAIT strategy

The active 6.19.14-ovo patch follows Nika's VM-exit fast path. After checking
VM-entry failure, pending IDT vectoring, STI/MOV-SS blocking, and the Trap
Flag, MONITOR and MWAIT exits advance the guest RIP and immediately re-enter
the guest. This treats both instructions as fast no-ops. The patched modules
build successfully and the Windows VM cold-boots without a host lockup.

That result proves boot stability, not complete instruction semantics. The
fast path does not register a monitored address or put the vCPU to sleep, and
the guest CPUID currently keeps MONITOR/MWAIT hidden.

An alternative retained for future testing is KVM's native execution path:

```text
KVM_X86_DISABLE_EXITS_MWAIT
QEMU: -overcommit cpu-pm=on
```

That path clears the VMCS MONITOR/MWAIT exiting controls and lets hardware
provide the real monitor, wait, and wake behavior. It should only be tested
with dedicated one-to-one vCPU pinning, no CPU overcommit, matching host CPU
capabilities, and consistent CPUID leaf 1/leaf 5 exposure. It is not enabled
in the current baseline. Do not enable it while evaluating the ASM fast path,
because native execution prevents the corresponding VM exits and therefore
bypasses the ASM code being tested.

## Historical Nika MSR passthrough experiment (retired OVO variant)

This section describes an older Architectural LBR variant, not today's
official patch. The official patch retains original Nika MSR passthrough.

Nika's complete MSR passthrough batch was tested on the Architectural LBR
baseline:

```text
identity/status: C80, DB2, 34, 3A, 19A, 639, 179 (read only)
machine check:   400-47F (read only)
PMU:             C1-C8 and 186-18D (read/write)
legacy LBR:      1C9 (read/write) and 680-69F (read only)
```

The Windows guest reached the desktop transition and then stopped with
`SYSTEM_THREAD_EXCEPTION_NOT_HANDLED (0x7E)`. The batch was removed from the
active patch. It bypassed KVM's virtual PMU for the listed PMU registers and
exposed physical legacy-LBR registers alongside the virtual Architectural
LBR. This experiment failed in the tested configuration; the exact failing
instruction and MSR were not identified because no crash dump was available.
Do not attribute the failure to a specific group without further evidence.

## Historical XSAVE CPUID cache fix (retired OVO variant, 2026-09-05)

This experiment and its patch files have been retired. The official Nika-based
release does NOT include this XSAVE-specific fix; it preserves Nika here.

CPUID leaf 0xD bypasses the ASM cache and is dispatched directly to upstream
`kvm_emulate_cpuid()` before the custom handler can populate the cache. Other
leaves and the MONITOR/MWAIT path are unchanged by this fix.

Guest before/after CSV captures each contain 1638 rows across six vCPUs and
three passes, with successful affinity checks and XCR0 reads throughout:

| Field | Before | After |
| --- | --- | --- |
| XCR0 | 0x7 on all vCPUs | unchanged |
| CPUID.0D.0:EBX | 0x240 | 0x340 on all vCPUs |
| CPUID.0D.1:EBX | 0x240 or 0x340 | 0x350 on all vCPUs |

All other captured CPUID registers were unchanged; each CPU's three passes
matched. This validates the observed size mismatch fix, not all possible
XCR0/XSS transitions or the cause of the earlier 0x7E crash.

The removed pre-fix baseline was `patches/6.19.14-ovo.intel.before-xsave-cache.patch`.
The read-only Windows collector is `tools/collect_cpuid.cpp`. Compile in an
x64 Native Tools Command Prompt and collect without changing VM settings:

```bat
cl /nologo /W4 /O2 /EHsc /MT collect_cpuid.cpp /Fe:collect_cpuid.exe
collect_cpuid.exe > guest-cpuid-xcr0.csv 2> guest-cpuid-xcr0-errors.txt
echo %ERRORLEVEL%
```

The `6.19.rc6-1` RC package is built from upstream Linux `v6.19-rc6` rather
than a `cachyos-6.19.rc6-1` release archive. The installer uses that upstream
archive, reconstructs the package source with the pinned CachyOS base, BORE,
and DKMS-Clang patches, and verifies all BLAKE2 checksums before applying the
OVO patch.

Build cache stays under `~/.cache/ovo-kvm`.
