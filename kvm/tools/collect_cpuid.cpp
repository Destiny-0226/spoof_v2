#define _WIN32_WINNT 0x0601
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <intrin.h>
#include <immintrin.h>
#include <cstdio>

#ifndef _M_X64
#error Build with the x64 MSVC toolchain.
#endif

// Guard against an advertised feature whose actual instruction faults.
static const char* read_xcr0(unsigned __int64* value, DWORD* exception)
{
    int features[4];
    __cpuidex(features, 1, 0);
    *value = 0;
    *exception = 0;
    const unsigned required = (1u << 26) | (1u << 27);
    if ((static_cast<unsigned>(features[2]) & required) != required)
        return "not_advertised";
    __try {
        *value = _xgetbv(0);
        return "ok";
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        *exception = GetExceptionCode();
        return "exception";
    }
}

static void emit(WORD group, DWORD cpu, unsigned pass,
                 unsigned leaf, unsigned subleaf, int r[4])
{
    PROCESSOR_NUMBER before{}, after{};
    unsigned __int64 xcr0;
    DWORD exception;
    GetCurrentProcessorNumberEx(&before);
    const char* status = read_xcr0(&xcr0, &exception);
    __cpuidex(r, static_cast<int>(leaf), static_cast<int>(subleaf));
    GetCurrentProcessorNumberEx(&after);
    const bool valid = before.Group == group && after.Group == group &&
                       before.Number == cpu && after.Number == cpu;
    std::printf("%u,%lu,%u,0x%08x,0x%08x,0x%08x,0x%08x,0x%08x,0x%08x,%u,0x%016llx,%s,0x%08lx\n",
                static_cast<unsigned>(group), cpu, pass, leaf, subleaf,
                static_cast<unsigned>(r[0]), static_cast<unsigned>(r[1]),
                static_cast<unsigned>(r[2]), static_cast<unsigned>(r[3]),
                valid ? 1u : 0u, xcr0, status, exception);
    if (!valid) {
        std::fprintf(stderr, "ERROR: CPU affinity verification failed.\n");
        ExitProcess(2);
    }
}

static void collect(WORD group, DWORD cpu, unsigned pass)
{
    int r[4];
    emit(group, cpu, pass, 0, 0, r);
    const unsigned max_basic = static_cast<unsigned>(r[0]);
    const unsigned singles[] = {1, 5, 0xa, 0x1a, 0x1c};
    for (unsigned leaf : singles)
        if (leaf <= max_basic) emit(group, cpu, pass, leaf, 0, r);

    if (max_basic >= 4) {
        for (unsigned sub = 0; sub < 64; ++sub) {
            emit(group, cpu, pass, 4, sub, r);
            if ((r[0] & 31) == 0) break;
        }
    }
    if (max_basic >= 7) {
        emit(group, cpu, pass, 7, 0, r);
        unsigned last = static_cast<unsigned>(r[0]);
        if (last > 63) {
            std::fprintf(stderr, "WARNING: leaf 7 subleaf limit capped at 63.\n");
            last = 63;
        }
        for (unsigned sub = 1; sub <= last; ++sub)
            emit(group, cpu, pass, 7, sub, r);
    }
    const unsigned topology[] = {0xb, 0x1f};
    for (unsigned leaf : topology) {
        if (leaf > max_basic) continue;
        for (unsigned sub = 0; sub < 32; ++sub) {
            emit(group, cpu, pass, leaf, sub, r);
            if (((r[2] >> 8) & 255) == 0) break;
        }
    }
    // Capture all architectural XSAVE component indices, including zero entries.
    if (max_basic >= 0xd)
        for (unsigned sub = 0; sub < 64; ++sub)
            emit(group, cpu, pass, 0xd, sub, r);

    emit(group, cpu, pass, 0x80000000u, 0, r);
    const unsigned max_ext = static_cast<unsigned>(r[0]);
    for (unsigned leaf = 0x80000001u; leaf <= 0x80000008u; ++leaf)
        if (leaf <= max_ext) emit(group, cpu, pass, leaf, 0, r);
}

int main()
{
    const WORD groups = GetActiveProcessorGroupCount();
    if (!groups) return 1;
    std::printf("group,cpu,pass,leaf,subleaf,eax,ebx,ecx,edx,affinity_ok,xcr0,xcr0_status,xcr0_exception\n");
    for (WORD group = 0; group < groups; ++group) {
        const DWORD count = GetActiveProcessorCount(group);
        if (!count || count > 64) return 1;
        for (DWORD cpu = 0; cpu < count; ++cpu) {
            GROUP_AFFINITY target{}, previous{};
            target.Group = group;
            target.Mask = static_cast<KAFFINITY>(1) << cpu;
            if (!SetThreadGroupAffinity(GetCurrentThread(), &target, &previous)) {
                std::fprintf(stderr, "ERROR: bind group %u CPU %lu: %lu\n",
                             static_cast<unsigned>(group), cpu, GetLastError());
                return 1;
            }
            for (unsigned pass = 0; pass < 3; ++pass)
                collect(group, cpu, pass);
            if (!SetThreadGroupAffinity(GetCurrentThread(), &previous, nullptr)) {
                std::fprintf(stderr, "ERROR: affinity restore failed: %lu\n", GetLastError());
                return 1;
            }
        }
    }
    return std::fflush(stdout) == 0 && !std::ferror(stdout) ? 0 : 1;
}
