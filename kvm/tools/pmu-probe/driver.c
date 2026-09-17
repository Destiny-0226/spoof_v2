#include <ntddk.h>
#include <wdmsec.h>
#include <intrin.h>
#include "protocol.h"

static const GUID DeviceClass =
    {0x90de9f27, 0xe078, 0x46ba, {0xa2, 0x9e, 0x38, 0x90, 0x50, 0x69, 0xc3, 0x27}};
static UNICODE_STRING DeviceName = RTL_CONSTANT_STRING(L"\\Device\\OvoPmuProbe");
static UNICODE_STRING LinkName = RTL_CONSTANT_STRING(L"\\DosDevices\\OvoPmuProbe");

DRIVER_INITIALIZE DriverEntry;
DRIVER_UNLOAD ProbeUnload;
DRIVER_DISPATCH ProbeDispatch;

static ULONG ReadMsr(ULONG index, ULONGLONG *value)
{
    __try {
        *value = __readmsr(index);
        return STATUS_SUCCESS;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return (ULONG)GetExceptionCode();
    }
}

static ULONG ReadPmc(ULONG index, ULONGLONG *value)
{
    __try {
        *value = __readpmc(index);
        return STATUS_SUCCESS;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return (ULONG)GetExceptionCode();
    }
}

static NTSTATUS Sample(const OVO_REQUEST *request, OVO_SAMPLE *result)
{
    GROUP_AFFINITY target = {0}, previous;
    PROCESSOR_NUMBER before = {0}, after = {0};
    KIRQL oldIrql;
    int leaf[4];
    ULONG count;

    if (request->version != OVO_PROBE_VERSION || request->fixed_counter > 1 ||
        request->group >= KeQueryActiveGroupCount() || request->cpu >= 64 ||
        request->cpu >= KeQueryActiveProcessorCountEx(request->group))
        return STATUS_INVALID_PARAMETER;
    if (KeGetCurrentIrql() != PASSIVE_LEVEL)
        return STATUS_INVALID_DEVICE_STATE;

    RtlZeroMemory(result, sizeof(*result));
    result->version = OVO_PROBE_VERSION;
    result->size = sizeof(*result);
    result->selector = request->fixed_counter ? 0x40000000 : 0;
    result->msr = request->fixed_counter ? 0x309 : 0xc1;
    result->msr_before_status = (ULONG)STATUS_NOT_SUPPORTED;
    result->rdpmc_status = (ULONG)STATUS_NOT_SUPPORTED;
    result->msr_after_status = (ULONG)STATUS_NOT_SUPPORTED;

    target.Group = request->group;
    target.Mask = ((KAFFINITY)1) << request->cpu;
    KeSetSystemGroupAffinityThread(&target, &previous);
    oldIrql = KeRaiseIrqlToDpcLevel();
    /* A bounded read-only sample: no loops, MSR writes, or interrupt disabling. */
    __try {
        KeGetCurrentProcessorNumberEx(&before);
        result->group = before.Group;
        result->cpu_before = before.Number;
        result->cr4 = __readcr4();
        if (before.Group != request->group || before.Number != request->cpu) {
            result->setup_status = (ULONG)STATUS_INVALID_DEVICE_STATE;
            __leave;
        }
        __cpuidex(leaf, 0, 0);
        if (leaf[0] < 0xa) {
            result->setup_status = (ULONG)STATUS_NOT_SUPPORTED;
            __leave;
        }
        __cpuidex(leaf, 0xa, 0);
        result->pmu_eax = (ULONG)leaf[0];
        result->pmu_edx = (ULONG)leaf[3];
        count = request->fixed_counter ? ((ULONG)leaf[3] & 0x1f) :
            (((ULONG)leaf[0] >> 8) & 0xff);
        result->width = request->fixed_counter ? (((ULONG)leaf[3] >> 5) & 0xff) :
            (((ULONG)leaf[0] >> 16) & 0xff);
        if (((ULONG)leaf[0] & 0xff) < 2 || !count ||
            !result->width || result->width > 64) {
            result->setup_status = (ULONG)STATUS_NOT_SUPPORTED;
            __leave;
        }
        result->setup_status = ReadMsr(0x38f, &result->global_ctrl);
        if (result->setup_status)
            __leave;
        result->setup_status = ReadMsr(request->fixed_counter ? 0x38d : 0x186,
                                      &result->event_ctrl);
        if (result->setup_status)
            __leave;
        _mm_lfence();
        result->msr_before_status = ReadMsr(result->msr, &result->msr_before);
        _mm_lfence();
        result->rdpmc_status = ReadPmc(result->selector, &result->rdpmc);
        _mm_lfence();
        result->msr_after_status = ReadMsr(result->msr, &result->msr_after);
        _mm_lfence();
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        result->setup_status = (ULONG)GetExceptionCode();
    }
    KeGetCurrentProcessorNumberEx(&after);
    result->cpu_after = after.Number;
    if (after.Group != before.Group || after.Number != before.Number)
        result->setup_status = (ULONG)STATUS_INVALID_DEVICE_STATE;
    KeLowerIrql(oldIrql);
    KeRevertToUserGroupAffinityThread(&previous);
    return STATUS_SUCCESS;
}

NTSTATUS ProbeDispatch(PDEVICE_OBJECT device, PIRP irp)
{
    PIO_STACK_LOCATION stack = IoGetCurrentIrpStackLocation(irp);
    NTSTATUS status = STATUS_INVALID_DEVICE_REQUEST;
    ULONG_PTR bytes = 0;
    UNREFERENCED_PARAMETER(device);

    if (stack->MajorFunction == IRP_MJ_CREATE || stack->MajorFunction == IRP_MJ_CLOSE ||
        stack->MajorFunction == IRP_MJ_CLEANUP) {
        status = STATUS_SUCCESS;
    } else if (stack->MajorFunction == IRP_MJ_DEVICE_CONTROL &&
               stack->Parameters.DeviceIoControl.IoControlCode == IOCTL_OVO_SAMPLE) {
        if (stack->Parameters.DeviceIoControl.InputBufferLength != sizeof(OVO_REQUEST) ||
            stack->Parameters.DeviceIoControl.OutputBufferLength < sizeof(OVO_SAMPLE)) {
            status = STATUS_INFO_LENGTH_MISMATCH;
        } else {
            OVO_REQUEST request = *(OVO_REQUEST *)irp->AssociatedIrp.SystemBuffer;
            status = Sample(&request, (OVO_SAMPLE *)irp->AssociatedIrp.SystemBuffer);
            if (NT_SUCCESS(status))
                bytes = sizeof(OVO_SAMPLE);
        }
    }
    irp->IoStatus.Status = status;
    irp->IoStatus.Information = bytes;
    IoCompleteRequest(irp, IO_NO_INCREMENT);
    return status;
}

VOID ProbeUnload(PDRIVER_OBJECT driver)
{
    IoDeleteSymbolicLink(&LinkName);
    IoDeleteDevice(driver->DeviceObject);
}

NTSTATUS DriverEntry(PDRIVER_OBJECT driver, PUNICODE_STRING registryPath)
{
    PDEVICE_OBJECT device;
    NTSTATUS status;
    ULONG i;
    UNREFERENCED_PARAMETER(registryPath);
    for (i = 0; i <= IRP_MJ_MAXIMUM_FUNCTION; ++i)
        driver->MajorFunction[i] = ProbeDispatch;
    driver->DriverUnload = ProbeUnload;
    status = IoCreateDeviceSecure(driver, 0, &DeviceName, FILE_DEVICE_UNKNOWN,
        FILE_DEVICE_SECURE_OPEN, FALSE, &SDDL_DEVOBJ_SYS_ALL_ADM_ALL,
        &DeviceClass, &device);
    if (!NT_SUCCESS(status))
        return status;
    status = IoCreateSymbolicLink(&LinkName, &DeviceName);
    if (!NT_SUCCESS(status)) {
        IoDeleteDevice(device);
        return status;
    }
    device->Flags &= ~DO_DEVICE_INITIALIZING;
    return STATUS_SUCCESS;
}
