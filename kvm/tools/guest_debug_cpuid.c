// SPDX-License-Identifier: GPL-2.0-only
/*
 * Link with Linux 7.1.3 tools/testing/selftests/kvm (see kvm/README.md).
 * Unlike debug_regs, this never enables KVM_SET_GUEST_DEBUG. The guest owns
 * TF and DR0-DR3, exercising the lazy debug-register synchronization path.
 * The simultaneous BS/Bn expectation is specific to the Intel OVO patch.
 */
#include "kvm_util.h"
#include "processor.h"

#define TEST_DR6_BS (1ull << 14)
#define TEST_TF (1ull << 8)
#define TEST_DR7_FIXED 0x400ull

extern unsigned char ovo_cpuid_trap_target;
static volatile u64 observed_dr6, observed_rip, hits;

static void guest_db_handler(struct ex_regs *regs)
{
	u64 dr6;

	asm volatile("mov %%dr6, %0" : "=r" (dr6));
	observed_dr6 = dr6;
	observed_rip = regs->rip;
	hits++;
	regs->rflags &= ~TEST_TF;
	asm volatile("mov %0, %%dr7" : : "r" (TEST_DR7_FIXED) : "memory");
}

static __attribute__((__noinline__, __noclone__)) void guest_probe(u64 dr7, u64 tf)
{
	asm volatile(
		"lea ovo_cpuid_trap_target(%%rip), %%rax\n\t"
		"mov %%rax, %%dr0\n\t"
		"mov %%rax, %%dr1\n\t"
		"mov %%rax, %%dr2\n\t"
		"mov %%rax, %%dr3\n\t"
		"mov %[dr7], %%dr7\n\t"
		"mov $0xffff0ff0, %%eax\n\t"
		"mov %%rax, %%dr6\n\t"
		"xor %%eax, %%eax\n\t"
		"xor %%ecx, %%ecx\n\t"
		"pushfq\n\t"
		"or %[tf], (%%rsp)\n\t"
		"popfq\n\t"
		"cpuid\n\t"
		".global ovo_cpuid_trap_target\n"
		"ovo_cpuid_trap_target: nop\n\t"
		: : [dr7] "r" (dr7), [tf] "r" (tf)
		: "rax", "rbx", "rcx", "rdx", "cc", "memory");
}

static void guest_code(u64 dr7, u64 tf, u64 expected)
{
	/* Resolve lazy CPUID state before the actual debug-register experiment. */
	u32 a, b, c, d;
	__cpuid(0, 0, &a, &b, &c, &d);
	guest_probe(dr7, tf);
	GUEST_ASSERT_EQ(hits, 1);
	GUEST_ASSERT_EQ(observed_rip, (u64)&ovo_cpuid_trap_target);
	GUEST_ASSERT_EQ(observed_dr6 & (TEST_DR6_BS | 0xf), expected);
	GUEST_DONE();
}

int main(void)
{
	int test;

	TEST_REQUIRE(host_cpu_is_intel);
	for (test = 0; test < 17; test++) {
		struct kvm_vcpu *vcpu;
		struct kvm_vm *vm;
		struct ucall uc;
		u64 dr7 = TEST_DR7_FIXED;
		u64 tf = TEST_TF, expected = TEST_DR6_BS;

		if (test) {
			unsigned slot = (test - 1) % 8;

			dr7 |= 1ull << slot;
			tf = test > 8 ? TEST_TF : 0;
			expected = (1ull << (slot / 2)) | (tf ? TEST_DR6_BS : 0);
		}
		vm = vm_create_with_one_vcpu(&vcpu, guest_code);
		vm_install_exception_handler(vm, DB_VECTOR, guest_db_handler);
		vcpu_args_set(vcpu, 3, dr7, tf, expected);
		vcpu_run(vcpu);
		switch (get_ucall(vcpu, &uc)) {
		case UCALL_DONE:
			break;
		case UCALL_ABORT:
			REPORT_GUEST_ASSERT(uc);
			break;
		default:
			TEST_FAIL("case %d: unexpected exit %u", test, vcpu->run->exit_reason);
		}
		kvm_vm_free(vm);
		printf("PASS guest-owned debug case %d (TF=%llu DR7=%#llx)\n",
		       test, (unsigned long long)!!tf, (unsigned long long)dr7);
	}
	return 0;
}
