#include <windows.h>
#include <stdio.h>
#include "protocol.h"

int main(void)
{
    HANDLE device;
    WORD group, groups = GetActiveProcessorGroupCount();
    ULONG cpu, kind, pass;
    int errors = 0;
    device = CreateFileW(L"\\\\.\\OvoPmuProbe", GENERIC_READ, 0, NULL,
                         OPEN_EXISTING, 0, NULL);
    if (device == INVALID_HANDLE_VALUE) {
        fprintf(stderr, "Open device failed: %lu (load the driver; run elevated)\n",
                GetLastError());
        return 1;
    }
    puts("pass,group,cpu_before,cpu_after,kind,width,cr4,global_ctrl,event_ctrl,msr_before,rdpmc,msr_after,setup_status,before_status,rdpmc_status,after_status,bracket");
    for (pass = 0; pass < 3; ++pass) {
        for (group = 0; group < groups; ++group) {
            DWORD count = GetActiveProcessorCount(group);
            for (cpu = 0; cpu < count; ++cpu) {
                for (kind = 0; kind < 2; ++kind) {
                    OVO_REQUEST request = {OVO_PROBE_VERSION, group, (USHORT)cpu, kind};
                    OVO_SAMPLE s = {0};
                    DWORD bytes = 0;
                    const char *verdict = "error";
                    if (!DeviceIoControl(device, IOCTL_OVO_SAMPLE, &request,
                            sizeof(request), &s, sizeof(s), &bytes, NULL)) {
                        fprintf(stderr, "IOCTL group=%u cpu=%lu: %lu\n",
                                group, cpu, GetLastError());
                        ++errors;
                        continue;
                    }
                    if (bytes != sizeof(s) || s.size != sizeof(s) ||
                        s.version != OVO_PROBE_VERSION || s.group != group ||
                        s.cpu_before != cpu || s.cpu_after != cpu) {
                        fprintf(stderr, "Invalid response or CPU mismatch\n");
                        ++errors;
                        continue;
                    }
                    if (!s.setup_status && !s.msr_before_status &&
                        !s.rdpmc_status && !s.msr_after_status && s.width && s.width <= 64) {
                        ULONGLONG mask = s.width == 64 ? ~0ULL : (1ULL << s.width) - 1;
                        ULONGLONG span = (s.msr_after - s.msr_before) & mask;
                        ULONGLONG delta = (s.rdpmc - s.msr_before) & mask;
                        verdict = delta <= span ? "inside" : "outside";
                    } else {
                        ++errors;
                    }
                    printf("%lu,%lu,%lu,%lu,%s,%lu,%016llx,%016llx,%016llx,%016llx,%016llx,%016llx,%08lx,%08lx,%08lx,%08lx,%s\n",
                        pass, s.group, s.cpu_before, s.cpu_after, kind ? "fixed0" : "gp0",
                        s.width, s.cr4, s.global_ctrl, s.event_ctrl,
                        s.msr_before, s.rdpmc, s.msr_after,
                        s.setup_status, s.msr_before_status, s.rdpmc_status,
                        s.msr_after_status, verdict);
                }
            }
        }
        Sleep(100);
    }
    CloseHandle(device);
    return errors ? 2 : 0;
}
