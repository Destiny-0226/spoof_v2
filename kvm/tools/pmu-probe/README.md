# OVO read-only PMU probe

Windows x64 WDM test driver and administrator-only console client. Build with
`build.cmd` using VS 2026 and SDK/WDK build 28000. Build output is unsigned;
the script does not install certificates, register services, or modify BCD.

The only IOCTL samples general counter 0 or fixed counter 0 on a validated
processor group/CPU. It reads CR4, CPUID.A, the relevant controls, and a bounded
RDMSR / RDPMC / RDMSR sequence. No arbitrary MSR index, memory access, MSR write,
or CR4 change is exposed. The device ACL permits only SYSTEM and administrators.
The request thread affinity is restored after sampling. Dispatch-level sampling
prevents ordinary thread scheduling but does not exclude higher-priority
interrupts, host scheduling, or another PMU owner.

The client samples all active CPUs three times and emits CSV. `inside` means
the RDPMC value lies between the MSR samples modulo the declared counter width;
it is not a blanket PMU pass. All-zero inactive counters are inconclusive.
`outside` requires controlled reproduction and confirmation that no other owner
reconfigured the PMU; it is not automatically evidence of a KVM defect.

Initial scope: compile and inspect artifacts, then arrange explicit test signing
and a demand-start service before loading. Do not bypass signature enforcement.
Keep the existing stable KVM patch and Guest boot/security settings unchanged
until the loading procedure is agreed. No driver has to be installed to compile.

## Build verification: 2026-09-05

Observed over SSH in the authorized Windows Guest:

- Windows 10.0.26200.6725 (not the earlier 26100 baseline).
- VS Community 2026 Insiders 18.10.12120.281; MSVC 14.51.36231.
- SDK/WDK 10.0.28000.0 include/lib directories present, including ntddk.h
  and x64 ntoskrnl.lib. These directory names do not establish the QFE version.
- Secure Boot False; VBS status 0; BCD debug Yes, no explicit testsigning entry.
- `build.cmd`: exit 0 with /W4 /WX for both source files.
- Unsigned driver: 25088 bytes, SHA256
  `EF5DF8359A7D85348CCC0323956B3784450D50C01E78BC0A02964E02CEB92E6F`.
- Client: 143872 bytes. With no driver loaded, prints `Open device failed: 2`
  and exits 1 as expected.

Guest source/build directory: `C:\Users\XOS\ovo-pmu-probe-v1`.
No driver service, certificate, BCD change, or restart was performed.
Kernel sampling and MSR/RDPMC agreement remain UNTESTED pending signed loading.

## Test-signing preparation: 2026-09-05

With explicit user approval, `prepare-test.ps1` was run in the Guest. The
28000 SignTool executable was absent, so the installed SDK 26100 x64 SignTool
was used (the driver build still used 28000 headers/libraries).

- Created a six-month, non-exportable private key in CurrentUser/My for
  `CN=OVO PMU Probe Test Only`; only its public certificate was exported.
- Certificate thumbprint: `DA8F30B08F522FE8ABE186AFC4A4B729A74AD480`.
- Added this certificate to LocalMachine/Root and LocalMachine/TrustedPublisher.
- Signed a separate copy; SignTool /pa verification passed, Authenticode Valid.
  This verifies the test signature, not production kernel signing eligibility.
- Installed signed file: `C:\Windows\System32\drivers\ovo-pmu-probe.sys`.
- Signed file SHA256:
  `90A504F3045C259C3897E08EE91ED0427C73D51C363CAAB648C0E5729D760D88`.
- Registered `OvoPmuProbe` as KERNEL_DRIVER, DEMAND_START; verified STOPPED.
- Set current BCD entry testsigning Yes; restart pending, not performed.
- Original BCD export, before/after text, public certificate and state.json:
  `C:\Users\XOS\ovo-pmu-probe-v1\test-signing`.

Do not rerun preparation on this existing state. After the user's manual
restart, verify boot state before explicitly starting the driver. No kernel
sampling has run yet.

## First loaded read-only sample: 2026-09-05

After the manually requested Guest restart, `testsigning Yes` and the signed
driver's Valid Authenticode state were confirmed. `OvoPmuProbe` was explicitly
started, sampled, and then explicitly stopped. It remains DEMAND_START and
STOPPED. The Guest CSV is
`C:\Users\XOS\ovo-pmu-probe-v1\results.csv`, SHA256
`3071F30609AF0E2A4C8EA87F4D480B50C91F15A7DF3D17462456A503B6499D71`.

All 36 samples reported group 0 and the requested CPU before/after, CR4
`0xB50EF8`, PMU width 48, and successful RDMSR/RDPMC status fields. No test
counter had been configured after reboot, so virtual PMU controls and virtual
MSR counter values were zero. The decisive repeatable observation was on vCPU0
for fixed counter 0: `RDMSR(0x309)=0`, `RDPMC(0x40000000)=0x800000015CCF`,
then `RDMSR(0x309)=0`; the same nonzero RDPMC result appeared in all three
passes. The other sampled vCPUs returned zero for this selector.

This is an interface-consistency failure for the inactive virtual fixed
counter: an RDPMC value outside the bracketing virtual MSR values must not be
treated as a virtual-PMU result. It is consistent with the active patch's
unconditional clearing of `CPU_BASED_RDPMC_EXITING`, but does not by itself
prove which host counter or scheduling state supplied the value. Do not infer
guest or host address disclosure beyond the observed counter value.

Next remediation candidate: remove that unconditional RDPMC-exit clearing and
use upstream KVM RDPMC emulation. Rebuild and rerun the same signed probe with
all PMU controls inactive before evaluating active-counter agreement. No patch
change has been made by this probe.

## RDPMC emulation retest: 2026-09-05

After removing the unconditional `CPU_BASED_RDPMC_EXITING` clearing, the
rebuilt Guest was sampled with the same read-only probe. The output is
`C:\Users\XOS\ovo-pmu-probe-v1\results-rdpmc-emulated.csv`, SHA256
`C10C99BFC016531491F82BCC70221363ED646928590BC12BD8A1BDBF69184CA5`.

All 36 samples completed successfully. For both GP0 and fixed0 on every
sampled vCPU, inactive PMU controls, bracketing RDMSR values, and RDPMC all
returned zero; every row reports `bracket=inside`. This removes the prior
observed inactive-fixed-counter inconsistency. The service was stopped after
sampling and remains demand-start.

For eventual rollback (not performed), stop/delete only service OvoPmuProbe,
then remove only its installed driver file. Delete the current entry's explicit
testsigning value with `bcdedit /deletevalue {current} testsigning` and restart
to restore the prior absent setting; quote `{current}` in PowerShell. Remove
only the recorded certificate thumbprint from the three stores above (and its
private key using the certificate provider's DeleteKey option). Do not reset
other BCD values or certificate stores. Retain source, test results and backups.
