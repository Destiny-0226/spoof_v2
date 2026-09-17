// SPDX-License-Identifier: MIT
// Independent CPUID latency probe. No detector thresholds or clock adjustment.
// Linux: c++ -std=c++17 -O2 -pthread cpuid_latency.cpp -o cpuid_latency
// Windows (x64 Native Tools): cl /std:c++17 /O2 /EHsc cpuid_latency.cpp
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <thread>
#include <vector>

#ifdef _WIN32
#define NOMINMAX
#include <windows.h>
#include <intrin.h>
#else
#include <pthread.h>
#include <sched.h>
#include <x86intrin.h>
#endif

using Tick = std::uint64_t;
static_assert(std::atomic<Tick>::is_always_lock_free,
              "This probe requires lock-free 64-bit stores (build for x64)");

struct Regs { unsigned a, b, c, d; };

static inline Regs cpuid(unsigned leaf, unsigned subleaf)
{
#ifdef _MSC_VER
    int r[4];
    __cpuidex(r, static_cast<int>(leaf), static_cast<int>(subleaf));
    return {unsigned(r[0]), unsigned(r[1]), unsigned(r[2]), unsigned(r[3])};
#else
    Regs r;
    // cpuid.h's non-volatile asm may be deleted when results are discarded.
    asm volatile("cpuid" : "=a"(r.a), "=b"(r.b), "=c"(r.c), "=d"(r.d)
                 : "a"(leaf), "c"(subleaf) : "memory");
    return r;
#endif
}

static inline void reference(bool serialize)
{
    if (serialize) {
#ifdef _MSC_VER
        _serialize(); _serialize(); _serialize();
#else
        // Encoding avoids requiring -mserialize for the whole executable.
        asm volatile(".byte 0x0f,0x01,0xe8\n\t"
                     ".byte 0x0f,0x01,0xe8\n\t"
                     ".byte 0x0f,0x01,0xe8" ::: "memory");
#endif
    } else {
        _mm_lfence(); _mm_lfence(); _mm_lfence(); _mm_lfence();
        _mm_lfence(); _mm_lfence(); _mm_lfence(); _mm_lfence();
    }
}

static bool pin(unsigned cpu)
{
#ifdef _WIN32
    GROUP_AFFINITY affinity{};
    affinity.Group = static_cast<WORD>(cpu / 64);
    affinity.Mask = static_cast<KAFFINITY>(1) << (cpu % 64);
    return SetThreadGroupAffinity(GetCurrentThread(), &affinity, nullptr) != 0;
#else
    if (cpu >= CPU_SETSIZE)
        return false;
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    return pthread_setaffinity_np(pthread_self(), sizeof(set), &set) == 0;
#endif
}

struct Counter {
    alignas(128) std::atomic<Tick> value{0};
    alignas(128) std::atomic<bool> stop{false};
    alignas(128) std::atomic<int> ready{0};
    std::thread worker;

    explicit Counter(unsigned cpu) : worker([this, cpu] {
        if (!pin(cpu)) {
            ready.store(-1, std::memory_order_release);
            return;
        }
        ready.store(1, std::memory_order_release);
        Tick n = 0;
        while (!stop.load(std::memory_order_relaxed)) {
            // One writer: relaxed stores compile to MOV, not LOCK instructions.
            for (unsigned i = 0; i < 256; ++i) {
                value.store(++n, std::memory_order_relaxed);
                value.store(++n, std::memory_order_relaxed);
                value.store(++n, std::memory_order_relaxed);
                value.store(++n, std::memory_order_relaxed);
                value.store(++n, std::memory_order_relaxed);
                value.store(++n, std::memory_order_relaxed);
                value.store(++n, std::memory_order_relaxed);
                value.store(++n, std::memory_order_relaxed);
            }
        }
    }) {}

    ~Counter()
    {
        stop.store(true, std::memory_order_relaxed);
        worker.join();
    }

    bool sync() const
    {
        const auto deadline = std::chrono::steady_clock::now() +
                              std::chrono::seconds(2);
        const Tick initial = value.load(std::memory_order_relaxed);
        unsigned spins = 0;
        while (value.load(std::memory_order_relaxed) == initial) {
            if ((++spins & 4095u) == 0 &&
                std::chrono::steady_clock::now() > deadline)
                return false;
        }
        return true;
    }
};

struct Distribution {
    Tick min, p10, median, p90;
};

static Distribution summarize(std::vector<Tick> &v)
{
    if (v.empty())
        throw std::runtime_error("no valid samples; check CPU affinity");
    std::sort(v.begin(), v.end());
    return {v.front(), v[(v.size() - 1) / 10], v[(v.size() - 1) / 2],
            v[(v.size() - 1) * 9 / 10]};
}

static void print(const char *clock, unsigned leaf, unsigned subleaf,
                  std::vector<Tick> &cp, std::vector<Tick> &ref)
{
    auto c = summarize(cp);
    auto r = summarize(ref);
    std::printf("%s,%08x,%08x,%zu,%llu,%llu,%llu,%llu,%llu,%.4f\n",
                clock, leaf, subleaf, cp.size(),
                (unsigned long long)c.min, (unsigned long long)c.p10,
                (unsigned long long)c.median, (unsigned long long)c.p90,
                (unsigned long long)r.p10, r.p10 ? double(c.p10) / r.p10 : 0.0);
}

template<bool Cpuid>
static Tick software_sample(const Counter &counter, unsigned leaf,
                            unsigned subleaf, bool serialize)
{
    const Tick before = counter.value.load(std::memory_order_relaxed);
    std::atomic_signal_fence(std::memory_order_seq_cst);
    if constexpr (Cpuid)
        (void)cpuid(leaf, subleaf);
    else
        reference(serialize);
    std::atomic_signal_fence(std::memory_order_seq_cst);
    const Tick after = counter.value.load(std::memory_order_relaxed);
    return after > before ? after - before : 0;
}

template<bool Cpuid>
static Tick tsc_sample(unsigned leaf, unsigned subleaf, bool serialize)
{
    unsigned aux;
    _mm_lfence();
    const Tick before = __rdtsc();
    _mm_lfence();
    std::atomic_signal_fence(std::memory_order_seq_cst);
    if constexpr (Cpuid)
        (void)cpuid(leaf, subleaf);
    else
        reference(serialize);
    std::atomic_signal_fence(std::memory_order_seq_cst);
    const Tick after = __rdtscp(&aux);
    _mm_lfence();
    if (after < before)
        throw std::runtime_error("TSC moved backwards");
    return after - before;
}

static unsigned number(const char *text)
{
    char *end;
    const auto n = std::strtoul(text, &end, 10);
    if (!*text || *end || *text == '-' || n > 1000000)
        throw std::runtime_error("invalid numeric argument");
    return static_cast<unsigned>(n);
}

static void transition_burst(unsigned iterations)
{
#ifdef _MSC_VER
    int r[4];
    for (unsigned i = 0; i < iterations; ++i)
        __cpuidex(r, 0, 0);
#else
    unsigned a, b, c, d;
    for (unsigned i = 0; i < iterations; ++i) {
        a = 0;
        c = 0;
        asm volatile("cpuid" : "+a"(a), "=b"(b), "+c"(c), "=d"(d)
                     : : "memory");
    }
#endif
}

int main(int argc, char **argv)
{
    try {
        if (argc >= 2 && !std::strcmp(argv[1], "--transition-burst")) {
            if (argc < 3 || argc > 4) {
                std::fprintf(stderr,
                             "Usage: %s --transition-burst CPU [ITERATIONS]\n",
                             argv[0]);
                return 2;
            }
            const unsigned cpu = number(argv[2]);
            const unsigned iterations = argc == 4 ? number(argv[3]) : 200000;
            if (iterations < 1000)
                throw std::runtime_error("use 1000..1000000 burst iterations");
            if (!pin(cpu))
                throw std::runtime_error("transition-burst CPU affinity failed");
            for (unsigned i = 0; i < 1000; ++i)
                (void)cpuid(0, 0);
            std::printf("# transition_burst_cpu=%u iterations=%u leaf=00000000 "
                        "subleaf=00000000\n", cpu, iterations);
            transition_burst(iterations);
            std::puts("# transition_burst_complete=1");
            return 0;
        }
        if (argc < 3 || argc > 4) {
            std::fprintf(stderr, "Usage: %s MEASUREMENT_CPU COUNTER_CPU [SAMPLES]\n"
                         "       %s --transition-burst CPU [ITERATIONS]\n"
                         "Use two separate physical cores of the same type.\n",
                         argv[0], argv[0]);
            return 2;
        }
        const unsigned measure = number(argv[1]), count = number(argv[2]);
        const unsigned samples = argc == 4 ? number(argv[3]) : 2000;
        if (measure == count || samples < 100 || samples > 100000)
            throw std::runtime_error("use distinct CPUs and 100..100000 samples");
        if (!pin(measure))
            throw std::runtime_error("measurement CPU affinity failed");
        const auto basic = cpuid(0, 0);
        const bool serialize = basic.a >= 7 && (cpuid(7, 0).d & (1u << 14));
        const bool rdtscp = cpuid(0x80000000, 0).a >= 0x80000001 &&
                            (cpuid(0x80000001, 0).d & (1u << 27));
        std::printf("# measurement_cpu=%u counter_cpu=%u serialize=%d rdtscp=%d\n",
                    measure, count, serialize, rdtscp);
        std::puts("# Raw distributions, NOT a VMAware pass/fail reproduction.");
        std::puts("clock,leaf,subleaf,samples,cpuid_min,cpuid_p10,cpuid_median,"
                  "cpuid_p90,reference_p10,p10_ratio");
        const unsigned leaves[][2] = {
            {0, 0}, {1, 0}, {7, 0}, {7, 1}, {0xa, 0}, {0xb, 0},
            {0xb, 255}, {0xd, 0}, {0xd, 1}, {0x1f, 0},
            {0x80000000, 0}, {0x80000001, 0}, {0x80000007, 0},
            {0x40000000, 0}, {0xffffffff, 0},
        };
        Counter counter(count);
        while (!counter.ready.load(std::memory_order_acquire))
            std::this_thread::yield();
        if (counter.ready.load(std::memory_order_acquire) < 0)
            throw std::runtime_error("counter CPU affinity failed");
        for (unsigned i = 0; i < 10000; ++i)
            reference(serialize);

        for (const auto &leaf : leaves) {
            std::vector<Tick> cp, ref, tc, tr;
            cp.reserve(samples); ref.reserve(samples);
            tc.reserve(samples); tr.reserve(samples);
            for (unsigned attempt = 0; cp.size() < samples &&
                 attempt < samples * 10; ++attempt) {
                Tick c, r;
                if (!counter.sync())
                    throw std::runtime_error("counter stalled; no measurement");
                // Alternate order instead of depending on a detector's sequence.
                if (attempt & 1) {
                    c = software_sample<true>(counter, leaf[0], leaf[1], serialize);
                    if (!counter.sync())
                        throw std::runtime_error("counter stalled");
                    r = software_sample<false>(counter, leaf[0], leaf[1], serialize);
                } else {
                    r = software_sample<false>(counter, leaf[0], leaf[1], serialize);
                    if (!counter.sync())
                        throw std::runtime_error("counter stalled");
                    c = software_sample<true>(counter, leaf[0], leaf[1], serialize);
                }
                if (c && r) { cp.push_back(c); ref.push_back(r); }
            }
            print("software_ticks", leaf[0], leaf[1], cp, ref);
            if (rdtscp) {
                for (unsigned i = 0; i < samples; ++i) {
                    // Separate windows: never introduce RDTSC into software timing.
                    tr.push_back(tsc_sample<false>(leaf[0], leaf[1], serialize));
                    tc.push_back(tsc_sample<true>(leaf[0], leaf[1], serialize));
                }
                print("tsc_ticks", leaf[0], leaf[1], tc, tr);
            }
        }
    } catch (const std::exception &e) {
        std::fprintf(stderr, "ERROR: %s\n", e.what());
        return 1;
    }
}
