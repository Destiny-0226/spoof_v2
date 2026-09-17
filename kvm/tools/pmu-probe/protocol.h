#pragma once

#define OVO_PROBE_VERSION 1
#define IOCTL_OVO_SAMPLE CTL_CODE(FILE_DEVICE_UNKNOWN, 0x800, METHOD_BUFFERED, FILE_READ_DATA)

typedef struct _OVO_REQUEST {
    ULONG version;
    USHORT group;
    USHORT cpu;
    ULONG fixed_counter;
} OVO_REQUEST;

typedef struct _OVO_SAMPLE {
    ULONG version;
    ULONG size;
    ULONG group;
    ULONG cpu_before;
    ULONG cpu_after;
    ULONG selector;
    ULONG msr;
    ULONG width;
    ULONG pmu_eax;
    ULONG pmu_edx;
    ULONG setup_status;
    ULONG msr_before_status;
    ULONG rdpmc_status;
    ULONG msr_after_status;
    ULONGLONG cr4;
    ULONGLONG global_ctrl;
    ULONGLONG event_ctrl;
    ULONGLONG msr_before;
    ULONGLONG rdpmc;
    ULONGLONG msr_after;
} OVO_SAMPLE;
